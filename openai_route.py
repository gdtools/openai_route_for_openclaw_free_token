import json
import os
import threading
import time
import html
import uuid
from typing import Any

import pymysql
import requests
from flask import Flask, Response, jsonify, request, stream_with_context
from pymysql.cursors import DictCursor


app = Flask(__name__)

EXPECTED_AUTH = os.getenv("OPENAI_PROXY_AUTH", "Bearer 你自己的key随便写前面的空格不要动")
DB_HOST = os.getenv("OPENAI_PROXY_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("OPENAI_PROXY_DB_PORT", "3306"))
DB_USER = os.getenv("OPENAI_PROXY_DB_USER", "openaidb")
DB_PASSWORD = os.getenv("OPENAI_PROXY_DB_PASSWORD", "openaidb")
DB_NAME = os.getenv("OPENAI_PROXY_DB_NAME", "openaidb")
DB_TABLE = os.getenv("OPENAI_PROXY_DB_TABLE", "models")
LOG_TABLE = os.getenv("OPENAI_PROXY_LOG_TABLE", "logs")
LISTEN_HOST = os.getenv("OPENAI_PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("OPENAI_PROXY_LISTEN_PORT", "8056"))


def open_db():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )


def reset_expired_cycle_usage(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE `{DB_TABLE}`
            SET USED_CYCLE_QTY = 0, USED_CYCLE_TOKENS = 0
            WHERE USED_CYCLE_QTY >0 and (
                LIMIT_TYPE = 'miao'
                OR (LIMIT_TYPE = 'fen' AND SECOND(NOW()) = 0)
                OR (LIMIT_TYPE = 'shi' AND SECOND(NOW()) = 0 AND MINUTE(NOW()) = 0)
                OR (LIMIT_TYPE = 'tian' AND SECOND(NOW()) = 0 AND MINUTE(NOW()) = 0 AND HOUR(NOW()) = 0)
                OR (
                        LIMIT_TYPE = 'yue'
                    AND SECOND(NOW()) = 0
                    AND MINUTE(NOW()) = 0
                    AND HOUR(NOW()) = 0
                    AND DAY(`CREATED_AT`) = DAY(NOW())
                )
                OR (
                        LIMIT_TYPE = 'nian'
                    AND SECOND(NOW()) = 0
                    AND MINUTE(NOW()) = 0
                    AND HOUR(NOW()) = 0
                    AND DAY(`CREATED_AT`) = DAY(NOW())
                    AND MONTH(`CREATED_AT`) = MONTH(NOW())
                )
               )
            """
        )
    conn.commit()


def start_reset_scheduler() -> threading.Thread:
    def loop() -> None:
        next_run = time.time()
        while True:
            next_run += 1
            conn = None
            try:
                conn = open_db()
                reset_expired_cycle_usage(conn)
            except Exception as exc:
                print("定时重置任务异常:", str(exc))
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            finally:
                if conn:
                    conn.close()
            sleep_seconds = next_run - time.time()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            else:
                next_run = time.time()

    thread = threading.Thread(target=loop, name="cycle-reset-scheduler", daemon=True)
    thread.start()
    return thread


def get_available_model(conn, model: str) -> dict[str, Any] | None:
    # 截取model前30位
    model = model[:50]
    if "," in model:
        groups = [item.strip() for item in model.split(",") if item.strip()]
        if not groups:
            return None
        escaped_groups = ", ".join(conn.escape(group) for group in groups)
        model_filter = f"`GROUP` IN ({escaped_groups})"
    elif len(model) <= 2:
        model_filter = f"`GROUP` = {conn.escape(model)}"
    else:
        model_filter = f"(`NAME` = {conn.escape(model)} or `MODEL` = {conn.escape(model)})"

    sql = f"""
        SELECT *
        FROM `{DB_TABLE}`
        WHERE {model_filter}
          AND ((LIMIT_QTY - USED_CYCLE_QTY) * LIMIT_QTY + (LIMIT_TOKENS - USED_CYCLE_TOKENS) * LIMIT_TOKENS) > 0
        ORDER BY USED_LATEST asc
        LIMIT 1
    """

    with conn.cursor() as cursor:

        cursor.execute(sql)
        rst = cursor.fetchone()
        return rst


def merge_force_parameter(user_post: dict[str, Any], model_row: dict[str, Any]) -> dict[str, Any]:

    payload = dict(user_post)
    payload["model"] = model_row.get("MODEL")
    force_parameter = model_row.get("FORCE_PARAMETER")
    if force_parameter is None:
        return payload
    if not str(force_parameter).strip():
        return payload
    force_payload: dict[str, Any] = {}
    try:
        parsed = json.loads(str(force_parameter))
    except json.JSONDecodeError:
        return payload
    if isinstance(parsed, dict):
        force_payload = dict(parsed)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                force_payload.update(item)
    if force_payload:
        payload.update(force_payload)

    return payload


def extract_total_tokens(usage: Any) -> int:
    if not usage:
        return 0
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump(mode="json")
    if not isinstance(usage, dict):
        return 0
    total_tokens = usage.get("total_tokens")
    if isinstance(total_tokens, int):
        return total_tokens
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        return prompt_tokens + completion_tokens
    return 0


def update_usage(conn, row_id: int, used_tokens: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE `{DB_TABLE}`
            SET USED_CYCLE_QTY = USED_CYCLE_QTY + 1,
                USED_ALL_QTY = USED_ALL_QTY + 1,
                USED_CYCLE_TOKENS = USED_CYCLE_TOKENS + %s,
                USED_ALL_TOKENS = USED_ALL_TOKENS + %s,
                USED_LATEST = NOW()
            WHERE ID = %s
            """,
            (used_tokens, used_tokens, row_id),
        )
    conn.commit()


def create_request_log(conn, model_id: int, request_payload: dict[str, Any]) -> str:
    log_uuid = uuid.uuid4().hex
    try:
        payload_json = json.dumps(request_payload, ensure_ascii=False, allow_nan=False)
    except ValueError:
        payload_json = json.dumps({"_raw": str(request_payload)}, ensure_ascii=False, allow_nan=False)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO `{LOG_TABLE}` (`UUID`, `MODEL_ID`, `REQUEST_AT`, `REQUEST_PAYLOAD`)
            VALUES (%s, %s, NOW(), %s)
            """,
            (log_uuid, model_id, payload_json),
        )
    conn.commit()
    return log_uuid


def finish_request_log(conn, log_uuid: str, finish_text: str | None) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE `{LOG_TABLE}`
            SET FINISH_AT = NOW(),
                FINISH_TEXT = %s
            WHERE UUID = %s
            """,
            (finish_text, log_uuid),
        )
    conn.commit()


def extract_finish_text_from_response(resp_json: Any) -> str:
    if not isinstance(resp_json, dict):
        return ""
    choices = resp_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = first.get("text")
    if isinstance(text, str):
        return text
    return ""


def extract_delta_text_from_chunk(chunk_dict: Any) -> str:
    if not isinstance(chunk_dict, dict):
        return ""
    choices = chunk_dict.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    return ""


def build_request_meta(model_row: dict[str, Any]) -> tuple[str, dict[str, str]]:
    # 命中模型ID
    print("命中模型ID",model_row.get("ID"))
    base_url = str(model_row.get("BASE_URL", "")).rstrip("/") + "/"
    api_key = str(model_row.get("API_KEY", "")).strip()
    auth_value = api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": auth_value,
    }
    return base_url + "chat/completions", headers


@app.get("/")
def model_overview():
    query_columns = [
        "NAME",
        "BASE_URL",
        "GROUP",
        "MODEL",
        "FORCE_PARAMETER",
        "LIMIT_TYPE",
        "LIMIT_QTY",
        "LIMIT_TOKENS",
        "USED_CYCLE_QTY",
        "USED_CYCLE_TOKENS",
        "USED_ALL_QTY",
        "USED_ALL_TOKENS",
        "USED_LATEST",
        "CREATED_AT",
        "UPDATED_AT",
    ]
    column_widths = {
        "NAME": "8%",
        "BASE_URL": "12%",
        "GROUP": "4%",
        "MODEL": "8%",
        "FORCE_PARAMETER": "12%",
        "LIMIT_TYPE": "5%",
        "LIMIT_QTY": "5%",
        "LIMIT_TOKENS": "6%",
        "USED_CYCLE_QTY": "6%",
        "USED_CYCLE_TOKENS": "7%",
        "USED_ALL_QTY": "6%",
        "USED_ALL_TOKENS": "7%",
        "USED_LATEST": "4%",
        "CREATED_AT": "4%",
        "UPDATED_AT": "4%",
    }
    select_sql = f"""
        SELECT `NAME`, `BASE_URL`, `GROUP`, `MODEL`, `FORCE_PARAMETER`, `LIMIT_TYPE`,
               `LIMIT_QTY`, `LIMIT_TOKENS`, `USED_CYCLE_QTY`, `USED_CYCLE_TOKENS`,
               `USED_ALL_QTY`, `USED_ALL_TOKENS`, `USED_LATEST`, `CREATED_AT`, `UPDATED_AT`
        FROM `{DB_TABLE}`
        ORDER BY `ID` ASC
    """
    conn = open_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(select_sql)
            rows = cursor.fetchall() or []
    finally:
        conn.close()
    header_html = "".join(f"<th>{html.escape(col)}</th>" for col in query_columns)
    colgroup_html = "".join(
        f"<col style='width:{column_widths.get(col, '6%')}'>" for col in query_columns
    )
    body_parts: list[str] = []
    for row in rows:
        cells = []
        for col in query_columns:
            value = row.get(col)
            text = "" if value is None else str(value)
            cells.append(f"<td>{html.escape(text)}</td>")
        body_parts.append("<tr>" + "".join(cells) + "</tr>")
    body_html = "".join(body_parts) if body_parts else f"<tr><td colspan='{len(query_columns)}'>暂无数据</td></tr>"
    page = f"""<!doctype html>
    <html lang="zh-CN">
    <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>模型概况</title>
    <style>
        :root {{
        color-scheme: dark;
        }}
        * {{
        box-sizing: border-box;
        }}
        body {{
        margin: 0;
        padding: 24px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
        background: #0f1115;
        color: #d7dbe3;
        }}
        h1 {{
        margin: 0 0 16px;
        font-size: 20px;
        font-weight: 600;
        color: #f2f5fa;
        }}
        .table-wrap {{
        width: 100%;
        overflow: auto;
        border: 1px solid #2a3040;
        border-radius: 10px;
        background: #151922;
        }}
        table {{
        width: 100%;
        border-collapse: collapse;
        table-layout: fixed;
        }}
        th, td {{
        padding: 10px 12px;
        border-bottom: 1px solid #242b3a;
        border-right: 1px solid #242b3a;
        text-align: left;
        font-size: 13px;
        white-space: normal;
        word-break: break-word;
        overflow-wrap: anywhere;
        vertical-align: top;
        }}
        th:last-child, td:last-child {{
        border-right: none;
        }}
        th {{
        position: sticky;
        top: 0;
        z-index: 1;
        background: #1d2432;
        color: #aeb8cc;
        font-weight: 600;
        }}
        tr:hover td {{
        background: #1a2130;
        }}
    </style>
    </head>
    <body>
    <h1>模型概况</h1>
    <div class="table-wrap">
        <table>
        <colgroup>{colgroup_html}</colgroup>
        <thead><tr>{header_html}</tr></thead>
        <tbody>{body_html}</tbody>
        </table>
    </div>
    </body>
    </html>"""
    return Response(page, content_type="text/html; charset=utf-8")


@app.post("/v1/chat/completions")
def proxy_chat_completions():
    auth = request.headers.get("Authorization", "")
    if auth != EXPECTED_AUTH:
        return jsonify({"error": {"message": "Unauthorized"}}), 401

    user_post = request.get_json(silent=True)
    if not isinstance(user_post, dict):
        return jsonify({"error": {"message": "Invalid JSON body"}}), 400

    model = str(user_post.get("model", "")).strip()
    if not model:
        return jsonify({"error": {"message": "model is required"}}), 400

    conn = open_db()
    try:
        # reset_expired_cycle_usage(conn)
        model_row = get_available_model(conn, model)
        if not model_row:
            conn.close()
            return jsonify({"error": {"message": "No available model endpoint"}}), 429
   
        payload = merge_force_parameter(user_post, model_row)

        if payload.get("stream",False) == True:

            request_url, request_headers = build_request_meta(model_row)
            log_uuid = create_request_log(conn, int(model_row["ID"]), payload)
            
            stream_resp = requests.post(
                request_url,
                headers=request_headers,
                json=payload,
                stream=True,
                timeout=(10, 600),
            )
            # 状态异常
            if stream_resp.status_code != 200:
                update_usage(conn, int(model_row["ID"]), 0)
                finish_request_log(conn, log_uuid, stream_resp.text[:100000])
                stream_resp.close()
                model_row = get_available_model(conn, model)
                if not model_row:
                    conn.close()
                    return jsonify({"error": {"message": "No available model endpoint"}}), 429
                payload = merge_force_parameter(user_post, model_row)
                request_url, request_headers = build_request_meta(model_row)
                log_uuid = create_request_log(conn, int(model_row["ID"]), payload)
                stream_resp = requests.post(
                    request_url,
                    headers=request_headers,
                    json=payload,
                    stream=True,
                    timeout=(10, 600),
                )

            if stream_resp.status_code != 200:
                update_usage(conn, int(model_row["ID"]), 0)
                finish_request_log(conn, log_uuid, stream_resp.text[:100000])
                content = stream_resp.content
                content_type = stream_resp.headers.get("Content-Type") or "application/json; charset=utf-8"
                stream_resp.close()
                conn.close()
                return Response(content, status=stream_resp.status_code, content_type=content_type)

            stream_resp.encoding = "utf-8"

            @stream_with_context
            def event_stream():
                used_tokens = 0
                finish_parts: list[str] = []
                try:
                    for line in stream_resp.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data = line[6:].strip()
                            if data == "[DONE]":
                                yield "data: [DONE]\n\n"
                                continue
                            try:
                                chunk_dict = json.loads(data)
                            except json.JSONDecodeError:
                                yield line + "\n\n"
                                continue
                            # usage
                            # print("用量：",json.dumps(chunk_dict.get("usage"), ensure_ascii=False))
                            usage_tokens = extract_total_tokens(chunk_dict.get("usage"))
                            if usage_tokens > 0:
                                used_tokens = usage_tokens
                            delta_text = extract_delta_text_from_chunk(chunk_dict)
                            if delta_text:
                                finish_parts.append(delta_text)
                            yield "data: " + json.dumps(chunk_dict, ensure_ascii=False) + "\n\n"
                            continue
                        yield line + "\n\n"
                finally:
                    stream_resp.close()
                    finish_text = "".join(finish_parts)
                    finish_request_log(conn, log_uuid, finish_text[:100000] if finish_text else "")
                    update_usage(conn, int(model_row["ID"]), used_tokens)
                    conn.close()

            return Response(event_stream(), content_type="text/event-stream; charset=utf-8")
        
        request_url, request_headers = build_request_meta(model_row)
        log_uuid = create_request_log(conn, int(model_row["ID"]), payload)
        result = requests.post(
            request_url,
            headers=request_headers,
            json=payload,
            timeout=(10, 120),
        )
        # 状态异常
        if result.status_code != 200:
            # 先更新这条模型的用量为0
            update_usage(conn, int(model_row["ID"]), 0)
            finish_request_log(conn, log_uuid, result.text[:100000])
            model_row = get_available_model(conn, model)
            if not model_row:
                conn.close()
                return jsonify({"error": {"message": "No available model endpoint"}}), 429
            payload = merge_force_parameter(user_post, model_row)
            request_url, request_headers = build_request_meta(model_row)
            log_uuid = create_request_log(conn, int(model_row["ID"]), payload)
            result = requests.post(
                request_url,
                headers=request_headers,
                json=payload,
                timeout=(10, 120),
            )
            if result.status_code != 200:
                update_usage(conn, int(model_row["ID"]), 0)
                finish_request_log(conn, log_uuid, result.text[:100000])
                content = result.content
                content_type = result.headers.get("Content-Type") or "application/json; charset=utf-8"
                conn.close()
                return Response(content, status=result.status_code, content_type=content_type)
        result_json = result.json()
        used_tokens = extract_total_tokens(result_json.get("usage"))
        finish_request_log(conn, log_uuid, extract_finish_text_from_response(result_json)[:100000])
        update_usage(conn, int(model_row["ID"]), used_tokens)
        conn.close()
        return jsonify(result_json)
    except Exception as exc:
        try:
            if "log_uuid" in locals():
                finish_request_log(conn, log_uuid, str(exc)[:100000])
            if "model_row" in locals() and model_row:
                update_usage(conn, int(model_row["ID"]), 0)
            conn.close()
        except Exception:
            pass
        return jsonify({"error": {"message": str(exc)}}), 502


@app.get("/v1/models")
def list_models():
    auth = request.headers.get("Authorization", "")
    if auth != EXPECTED_AUTH:
        return jsonify({"error": {"message": "Unauthorized"}}), 401

    select_sql = f"""
        SELECT `MODEL`, UNIX_TIMESTAMP(MIN(`CREATED_AT`)) AS `CREATED_TS`
        FROM `{DB_TABLE}`
        WHERE `MODEL` IS NOT NULL AND TRIM(`MODEL`) <> ''
        GROUP BY `MODEL`
        ORDER BY `MODEL` ASC
    """
    conn = open_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(select_sql)
            rows = cursor.fetchall() or []
    finally:
        conn.close()

    data = []
    now_ts = int(time.time())
    for row in rows:
        model_id = str(row.get("MODEL", "")).strip()
        if not model_id:
            continue
        created_raw = row.get("CREATED_TS")
        created_ts = int(created_raw) if isinstance(created_raw, (int, float)) else now_ts
        data.append(
            {
                "id": model_id,
                "object": "model",
                "created": created_ts,
                "owned_by": "openai-proxy",
            }
        )

    return jsonify({"object": "list", "data": data})


if __name__ == "__main__":
    start_reset_scheduler()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=True)

# OpenAI Proxy（基于 Flask + MySQL）

轻量级，集中管理OpenAI接口的路由（接口分发），根据大模型额配额动态调用。\
主要用来集中管理免费ai大模型，供openclaw调用

## 主要使用场景

- 大量免费ai大模型资源，都有限制，需要进行统一管理
- openclaw使用免费token的时候，只想配置统一接口
- 需要强行注入参数，比如：禁用推理模式（think）
- 拦截分析openclaw请求日志

## 功能特性

- 仅兼容 OpenAI 接口：`/v1/chat/completions`
- 模型可设置的参数
  - 为不同模型注入强制参数（比如强制禁用推理{"think": false}）
  - 频率限制类型（秒/分/时/天/月/年）
  - 频率限制
  - token限制
  - 模型分组
- 可指定模型调用（model），也可指定组调用（group）
- 统计模型调用次数和token用量
- 错误重试
- 日志记录


## 调用示例

```bash
curl -X POST "http://127.0.0.1:8056/v1/chat/completions" \
  -H "Authorization: Bearer 这里就是你自己设置的token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "1,2,3",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

## 常见问题

- openclaw里，base\_url是什么？<https://your-domain.com/v1>
- openclaw里，api\_key是什么？`Bearer `   后面的部分，比如`这里就是你自己设置的token`
- openclaw里，Model参数怎么设？
  - 直接写模型名，比如`qwen3.5`
  - 写模型分组，比如`1,2,3`
- 怎么强行禁用推理模式：各个模型不同，一般在`FORCE_PARAMETER`字段设置为

```json
{"think": false, "thinking": {"type": "disabled"}, "enable_thinking": false}
```

- 按月限制的`起始日`怎么设？CREATED\_AT 的日期改为起始日即可
- 有模型修改界面吗？没有，直接操作数据库
- 有使用情况页面吗？首页
- 跟openclaw同服务器，怎么获取宿主机ip？ `docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' openclaw容器ID`
  
## 数据库表结构

<details>
<summary>点击查看sql代码</summary>
  
```sql
CREATE TABLE `models` (
  `ID` int(10) UNSIGNED NOT NULL COMMENT '自增主键',
  `NAME` varchar(255) NOT NULL COMMENT '名字',
  `GROUP` tinyint(4) NOT NULL DEFAULT '1' COMMENT '模型分组 (1/2/3)',
  `BASE_URL` varchar(500) DEFAULT NULL COMMENT '基础 URL',
  `API_KEY` varchar(255) DEFAULT NULL COMMENT 'API 密钥',
  `MODEL` varchar(100) NOT NULL COMMENT '模型标识',
  `FORCE_PARAMETER` json DEFAULT NULL COMMENT '强制参数（json）',
  `LIMIT_TYPE` enum('miao','fen','shi','tian','yue','nian') NOT NULL DEFAULT 'tian' COMMENT '限制周期类型',
  `LIMIT_QTY` int(10) DEFAULT '0' COMMENT '周期内限制请求 (0 不限制)',
  `LIMIT_TOKENS` bigint(20) DEFAULT '0' COMMENT '周期内限制 token(0 不限制)',
  `USED_CYCLE_QTY` int(10) DEFAULT '0' COMMENT '周期内请求数',
  `USED_CYCLE_TOKENS` bigint(20) DEFAULT '0' COMMENT '周期内 token 消耗',
  `USED_ALL_QTY` bigint(20) DEFAULT '0' COMMENT '所有请求数',
  `USED_ALL_TOKENS` bigint(20) DEFAULT '0' COMMENT '所有 token 消耗',
  `USED_LATEST` datetime DEFAULT NULL COMMENT '上次调用时间',
  `CREATED_AT` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `UPDATED_AT` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型配置表';
CREATE TABLE `logs` (
  `UUID` varchar(50) NOT NULL,
  `MODEL_ID` tinyint(11) NOT NULL COMMENT '模型ID',
  `REQUEST_AT` datetime NOT NULL COMMENT '请求时间',
  `REQUEST_PAYLOAD` json DEFAULT NULL COMMENT '请求参数',
  `FINISH_AT` datetime DEFAULT NULL COMMENT '结束时间',
  `FINISH_TEXT` text COMMENT '返回文本'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型请求log';
```

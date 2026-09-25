---
name: weather
description: Get current weather and short-term forecasts without an API key; use when users ask about weather, temperature, rain, wind, or travel conditions.
when_to_use: 用户询问实时天气、未来预报、温度、降雨、风力或出行天气时使用。
metadata:
  beanagent:
    always: false
---

# Weather

使用免费公开服务查询实时天气和短期预报。实时信息必须查询外部服务，不得依赖模型知识猜测。

## 工具选择与执行环境

- 优先使用 `web_fetch` 请求下面的完整 HTTPS URL，无需本地 curl 或 Python。
- 只有专用工具不满足需求时才使用 shell；先核对本轮实际 Shell 和已验证程序路径。
- 下列 curl 示例用于说明请求；Windows CMD 使用 `curl.exe`，其他系统使用 `curl`。
- 使用 `-sS --connect-timeout 10 --max-time 30` 保留错误并限制等待，不关闭证书验证、不自动修改代理。
- 不默认拼接 `| python`。确需脚本解析时使用已验证的解释器绝对路径；应用备用环境只用于标准库任务，不替代项目依赖环境。

## wttr.in（首选）

简要天气：

```bash
curl -sS --connect-timeout 10 --max-time 30 "https://wttr.in/London?format=3"
```

紧凑字段：

```bash
curl -sS --connect-timeout 10 --max-time 30 "https://wttr.in/London?format=j1"
```

完整预报：

```bash
curl -sS --connect-timeout 10 --max-time 30 "https://wttr.in/London?T"
```

`format=j1` 返回结构化 JSON。避免在 CMD 中直接使用带成对百分号的格式串，防止被当作环境变量展开。

使用约束：

- 地名空格编码为 `+`，例如 `New+York`。
- `?m` 使用公制，`?u` 使用美制。
- `?1` 只看今天，`?0` 只看当前。
- 用户未说明地点且上下文无法确定时，先询问地点。
- 用户使用“明天”“周末”等相对日期时，在结果中写明解析后的具体日期。

## Open-Meteo（降级）

wttr.in 不可用或需要结构化 JSON 时，先确定地点经纬度，再查询 Open-Meteo：

```bash
curl -sS --connect-timeout 10 --max-time 30 "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.12&current_weather=true"
```

文档：https://open-meteo.com/en/docs

## 失败边界

- 网络请求失败、地点含糊或服务返回异常时明确说明。
- curl 退出码 35 只说明 TLS 握手失败，不能直接断定证书或代理根因；保留错误后再判断，不重复相同命令碰运气。
- 不把历史天气或模型常识包装成实时数据。
- 不输出与用户问题无关的大段原始 JSON。

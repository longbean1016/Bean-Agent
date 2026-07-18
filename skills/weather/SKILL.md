---
name: weather
description: Get current weather and short-term forecasts without an API key; use when users ask about weather, temperature, rain, wind, or travel conditions.
when_to_use: 用户询问实时天气、未来预报、温度、降雨、风力或出行天气时使用。
metadata:
  beanagent:
    always: false
    requires:
      bins: ["curl"]
---

# Weather

使用免费公开服务查询实时天气和短期预报。实时信息必须查询外部服务，不得依赖模型知识猜测。

## wttr.in（首选）

简要天气：

```bash
curl -s "wttr.in/London?format=3"
```

紧凑字段：

```bash
curl -s "wttr.in/London?format=%l:+%c+%t+%h+%w"
```

完整预报：

```bash
curl -s "wttr.in/London?T"
```

格式代码：`%c` 天气、`%t` 温度、`%h` 湿度、`%w` 风、`%l` 地点。

使用约束：

- 地名空格编码为 `+`，例如 `New+York`。
- `?m` 使用公制，`?u` 使用美制。
- `?1` 只看今天，`?0` 只看当前。
- 用户未说明地点且上下文无法确定时，先询问地点。
- 用户使用“明天”“周末”等相对日期时，在结果中写明解析后的具体日期。

## Open-Meteo（降级）

wttr.in 不可用或需要结构化 JSON 时，先确定地点经纬度，再查询 Open-Meteo：

```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.12&current_weather=true"
```

文档：https://open-meteo.com/en/docs

## 失败边界

- 网络请求失败、地点含糊或服务返回异常时明确说明。
- 不把历史天气或模型常识包装成实时数据。
- 不输出与用户问题无关的大段原始 JSON。

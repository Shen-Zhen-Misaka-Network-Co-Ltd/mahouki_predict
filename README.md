# Mahouki Predict 魔法期预测Bot

从群聊记录中自动提取用户自述的经期信息，建立个人周期档案，预测下次经期与排卵期，并在来潮前发送提醒。

## 功能

- **自动采集** — 接收聊天消息，累计至 10K 字符后调用 LLM 分析，识别自述经期记录
- **周期预测** — 根据历史记录计算平均周期长度和持续时间，预测下次经期和排卵期窗口
- **每日提醒** — 支持查询当天/明天的预测结果，可接入定时任务自动推送提醒
- **JSON 持久化** — 所有数据存储在 `periods.json`，随时迁移和备份

## 快速开始

### 1. 安装依赖

```bash
pip install httpx
```

### 2. 配置 API

编辑 `config.json`：

```json
{
  "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "buffer_size": 10000
}
```

`base_url` 可替换为任何 OpenAI 兼容接口（如 vLLM、Ollama、Azure OpenAI 等）。

### 3. 运行

```bash
python bot.py
```

### 4. 注入消息

添加测试数据（格式：`add 文本|用户ID|用户名|时间戳`）：

```
> add 已经疼的像条蛆了|7704333043|yunelleowo|2026-06-03T17:34:27Z
> add 还会吐胃酸|7704333043|yunelleowo|2026-06-03T17:34:45Z
> add 肚子疼|7702483813|kagari306|2026-06-03T17:42:25Z
```

手动添加历史记录（直接编辑 `periods.json` 或等缓冲区满后自动分析）：

```json
{
  "users": {
    "7704333043": {
      "username": "yunelleowo",
      "periods": [
        {"start": "2026-05-06", "end": "2026-05-09", "confidence": "auto"},
        {"start": "2026-06-03", "end": "2026-06-06", "confidence": "auto"}
      ]
    }
  }
}
```

### 5. 查询

```bash
> stats
  yunelleowo (7704333043): 2 periods, next ~2026-07-01

> predict 7704333043
  周期: 28天
  末次: 2026-06-03
  下次: 2026-07-01 — 2026-07-03
  排卵: 2026-06-17 — 2026-06-22

> reminders
  🟠 yunelleowo 明天可能来月经，提前准备好卫生用品
```

## 提醒机制

```python
from bot import CycleBot

bot = CycleBot()

# 获取今天的提醒列表
reminders = bot.get_reminders()  # 默认当天
# reminders = bot.get_reminders("2026-07-01")

# 注册回调（如推送到 Telegram / Discord）
def on_reminder(r):
    print(f"Send to user: {r['message']}")

bot.set_reminder_callback(on_reminder)

# 触发提醒
bot.fire_reminders()
```

用 cron 每天定时调用：

```
0 8 * * * cd /path/to/mahouki_predict && python -c "from bot import CycleBot; CycleBot().fire_reminders()"
```

## 集成到 Telegram Bot

```python
from bot import CycleBot

bot = CycleBot()

# 收到群消息时调用
async def on_group_message(event):
    bot.receive_message(
        text=event.raw_text,
        user_id=str(event.sender_id),
        username=event.sender.username or "",
        timestamp=event.date.isoformat(),
    )
```

## 数据格式

### periods.json

```json
{
  "users": {
    "<user_id>": {
      "username": "yunelleowo",
      "periods": [
        {
          "start": "2026-06-03",
          "end": "2026-06-06",
          "evidence": "消息原文",
          "confidence": "auto"
        }
      ]
    }
  }
}
```

### 预测输出

| 字段 | 说明 |
|------|------|
| `avg_cycle_days` | 平均周期长度（天） |
| `avg_duration_days` | 平均经期持续天数 |
| `next_period_start` | 预测下次来潮日期 |
| `next_period_end` | 预测结束日期 |
| `fertile_window_start` | 排卵期开始 |
| `fertile_window_end` | 排卵期结束 |

## 许可证

MIT

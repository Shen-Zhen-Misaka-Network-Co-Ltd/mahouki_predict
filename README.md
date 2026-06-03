# Mahouki Predict 魔法期预测Bot

从群聊记录中自动提取用户自述的经期信息，建立个人周期档案，预测下次经期与排卵期，并在来潮前发送提醒。

## 功能

- **自动采集** — 接收聊天消息，累计至 10K 字符后调用 LLM 分析，提取自述经期记录
- **手动管理** — 支持手动增删经期记录，随时纠偏
- **周期预测** — 使用统计方法计算平均周期、范围与标准差，预测下次经期和排卵窗口
- **每日提醒** — 查询当天/明天的预测结果，可接入定时任务自动推送
- **数据导出** — 一键导出 CSV
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
  "api_url": "https://api.openai.com/v1/chat/completions",
  "model": "gpt-4o-mini",
  "buffer_size": 10000
}
```

`api_url` 可替换为任何 OpenAI 兼容接口的完整 URL（vLLM、Ollama、Azure OpenAI、deepseek 等均可），只需保证是 `POST /chat/completions` 格式。

### 3. 运行

```bash
python bot.py
```

### 4. 注入消息

```text
> add 已经疼的像条蛆了|7704333043|yunelleowo|2026-06-03T17:34:27Z
> add 还会吐胃酸|7704333043|yunelleowo|2026-06-03T17:34:45Z
> add 肚子疼|7702483813|kagari306|2026-06-03T17:42:25Z
```

缓冲区（默认 10K 字符）满后自动调用 LLM 分析，提取经期自述并存入 `periods.json`。

### 5. 手动管理经期记录

```text
> period add 7704333043 2026-05-06 2026-05-09 yunelleowo    手动添加
> period add 7704333043 2026-06-03 2026-06-06 yunelleowo
> period list 7704333043                                  列出
> period del 7704333043 2026-05-06                         删除
```

### 6. 查询预测

```text
> stats
  yunelleowo (7704333043): 2 periods, cycle ~28d, next ~2026-07-01

> predict 7704333043
  yunelleowo (7704333043)
  ════════════════════════════════════
    周期记录: 2 次
    平均周期: 28 天 (范围 28–28 天, σ=0)
    平均持续: 3 天
    ─────────────────────────
    末次经期: 2026-06-03 → 2026-06-06
    下次预测: 2026-07-01 → 2026-07-03
    排卵窗口: 2026-06-17 → 2026-06-22

> reminders
  🟠 yunelleowo 明天可能来月经，提前准备好卫生用品

> export
  Exported to period_export.csv
```

## CLI 命令一览

| 命令 | 说明 |
|------|------|
| `add text\|uid\|name\|ts` | 投喂消息 |
| `flush` | 强制分析缓冲区 |
| `predict <uid>` | 预测用户周期 |
| `period add <uid> <start> [end] [name]` | 手动添加经期 |
| `period del <uid> <start>` | 删除经期 |
| `period list [uid]` | 列出经期 |
| `export` | 导出 CSV |
| `stats` | 概览 |
| `reminders [date]` | 查看提醒 |
| `fire [date]` | 触发提醒回调 |

## 提醒机制

```python
from bot import CycleBot

bot = CycleBot()

# 注册回调（如推送到 Telegram / Discord / Webhook）
def on_reminder(r):
    requests.post("https://your-webhook/notify", json=r)

bot.set_reminder_callback(on_reminder)

# 每天定时调用
bot.fire_reminders()
```

用 cron 每天定时触发：

```
0 8 * * * cd /path/to/mahouki_predict && python -c "from bot import CycleBot; CycleBot().fire_reminders()"
```

## 集成到 Telegram Bot

```python
from bot import CycleBot

bot = CycleBot()

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
    "7704333043": {
      "username": "yunelleowo",
      "periods": [
        {
          "start": "2026-06-03",
          "end": "2026-06-06",
          "evidence": "已经疼的像条蛆了",
          "confidence": "auto"
        }
      ]
    }
  }
}
```

### 预测输出字段

| 字段 | 说明 |
|------|------|
| `avg_cycle_days` | 平均周期长度（天） |
| `cycle_min_days` | 最短周期 |
| `cycle_max_days` | 最长周期 |
| `cycle_std_days` | 周期标准差 |
| `next_period_start` | 预测下次来潮日期 |
| `next_period_end` | 预测结束日期 |
| `fertile_window_start` | 排卵期开始 |
| `fertile_window_end` | 排卵期结束 |

## 文件结构

```
.
├── bot.py          # 主程序
├── config.json     # API 配置（已 gitignore）
├── periods.json    # 经期数据（已 gitignore）
├── buffer.json     # 消息缓冲区（已 gitignore）
└── README.md
```

## 许可证

MIT

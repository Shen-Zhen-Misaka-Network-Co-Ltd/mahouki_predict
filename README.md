# Mahouki Predict 魔法期预测 Telegram Bot

监听 Telegram 群聊消息，自动识别用户自述的经期信息，建立周期档案，预测下次经期与排卵期，并在来潮前发送提醒。

## 快速开始

### 1. 安装

```bash
pip install -r requirements.txt
```

### 2. 配置

编辑 `config.json`：

```json
{
  "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "api_url": "https://api.openai.com/v1/chat/completions",
  "model": "gpt-4o-mini",
  "buffer_size": 10000,
  "bot_token": "1234567890:ABCdefGHIjklmNOPqrstUVwxyz"
}
```

| 字段 | 说明 |
|------|------|
| `api_key` | LLM API 密钥 |
| `api_url` | OpenAI 兼容接口完整 URL |
| `model` | 模型名称 |
| `buffer_size` | 累计多少字符后自动分析 (默认 10K) |
| `bot_token` | Telegram Bot Token (从 @BotFather 获取) |

### 3. 运行

```bash
python bot.py
```

## 工作原理

1. Bot 加入群聊后，监听所有文本消息
2. 消息按用户累计到缓冲区，达到 `buffer_size` 后调用 LLM 分析
3. LLM 提取自述经期的消息（只统计用户说自己的情况），存入 `periods.json`
4. 根据历史记录预测下次经期和排卵窗口
5. `/remind` 查看当天/明天的提醒

## Telegram 指令

| 指令 | 说明 |
|------|------|
| `/predict` | 查看我自己的周期预测 |
| `/stats` | 查看所有用户概览 |
| `/remind` | 查看今天的提醒 |
| `/flush` | 强制分析缓冲区 |

## 数据文件

| 文件 | 说明 |
|------|------|
| `periods.json` | 经期记录 (gitignored) |
| `buffer.json` | 消息缓冲区 (gitignored) |

## 手动管理

如需手动增删经期记录，直接编辑 `periods.json`：

```json
{
  "users": {
    "7704333043": {
      "username": "yunelleowo",
      "periods": [
        {"start": "2026-05-06", "end": "2026-05-09", "evidence": "", "confidence": "manual"},
        {"start": "2026-06-03", "end": "2026-06-06", "evidence": "已经疼的像条蛆了", "confidence": "auto"}
      ]
    }
  }
}
```

或通过 Python 调用：

```python
from bot import engine
engine.add_period("7704333043", "2026-07-01", "2026-07-03", "yunelleowo")
```

## 定时提醒

用 cron 每天执行一次推送（需在代码中集成 Telegram 推送逻辑）：

```bash
0 9 * * * cd /path/to/mahouki_predict && python -c "from bot import engine; print(engine.get_reminders())"
```

## 文件结构

```
├── bot.py              # Telegram Bot 主程序
├── config.json         # API & Bot 配置
├── periods.json        # 经期数据 (自动生成)
├── buffer.json         # 消息缓冲区 (自动生成)
├── requirements.txt    # Python 依赖
└── README.md
```

## 许可证

MIT

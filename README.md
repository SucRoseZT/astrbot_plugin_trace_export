# astrbot_plugin_trace_export — 本地黑匣子日志导出

与 `astrbot_plugin_execution_trace`（给模型自查用的轻量版）互补：本插件是**给你（管理员/用户）排障用的高容量版本**，导出动作完全不经过 LLM，零 Token 消耗。

## 指令

| 指令 | 别名 | 作用 |
|------|------|------|
| `/export_trace` | `/导出轨迹` | 将当前会话最近 100 条执行日志打包成 `trace_export_时间戳.txt` 直接发送到聊天框 |
| `/clear_trace` | `/清空轨迹` | 清空当前会话的黑匣子记录 |

## 记录的事件

- **用户消息**：发送者 + 文本前 100 字符（`event_message_type(ALL)` 监听）
- **LLM 输出**：回复文本前 200 字符（`on_llm_response`）
- **LLM 错误**：`resp.role == "err"` 时的报错（排查底层中断/重试的关键证据）
- **工具调用**：工具名 + JSON 参数（`on_using_llm_tool`，需 AstrBot > v4.23.1）
- **工具返回**：`ok:true` / `error` + 结果文本（`on_llm_tool_respond`，兼容 mcp `CallToolResult` 与旧版字符串）

## 发送文件的兼容性处理（重点）

1. 使用官方文档写法 `Comp.File(file=路径, name=文件名)` + `yield event.chain_result([...])`。
2. **NapCat / aiocqhttp 实战坑**：QQ 个人号协议端要求 File 组件额外携带
   `url="file:///<本地路径>"` 参数，否则报「文件消息缺少参数」。代码已自动附带。
3. 发送改用 `await event.send(event.chain_result(...))` 而非裸 `yield`——
   yield 的发送异常发生在处理函数之外无法捕获；`event.send` 同步执行，
   失败能被 try/except 接住，自动降级为纯文本（最近 15 条）并提示本地保存路径。

## 数据存储

- `self.trace_logs: dict[str, deque(maxlen=100)]`，键为 `unified_msg_origin`（私聊/群聊自动隔离）。
- 纯内存运行，**不注入大模型上下文、不修改 system_prompt**，零 Token；重启即清空。
- TXT 文件写入系统临时目录（`tempfile.gettempdir()`），UTF-8 编码。

## 局限

- File 组件在 QQ 官方接口、企业微信、钉钉等部分适配器上不受支持（文档明示），此时走降级文本路径。
- 仅记录内存，不落盘持久化（排障场景够用，历史依赖重启前导出）。

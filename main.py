import json
import os
import tempfile
import time
from collections import deque

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

MAX_RECORDS = 100
USER_MSG_LIMIT = 100
LLM_MSG_LIMIT = 200
DETAIL_LIMIT = 300
FALLBACK_TEXT_COUNT = 15


@register(
    "astrbot_plugin_trace_export",
    "QingYu",
    "本地黑匣子：高容量静默记录执行日志，/export_trace 零 Token 导出 TXT 文件，/clear_trace 清空",
    "1.0.0",
)
class TraceExportPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.trace_logs: dict = {}

    def _bucket(self, umo: str) -> deque:
        key = umo or "unknown"
        if key not in self.trace_logs:
            self.trace_logs[key] = deque(maxlen=MAX_RECORDS)
        return self.trace_logs[key]

    def _record(self, umo: str, kind: str, detail: str):
        try:
            self._bucket(umo).append(
                {
                    "time": time.strftime("%H:%M:%S"),
                    "kind": str(kind)[:32],
                    "detail": str(detail)[:DETAIL_LIMIT],
                }
            )
        except Exception as e:
            logger.warning(f"[trace_export] 记录日志失败(已忽略,不影响主流程): {e}")

    @staticmethod
    def _truncate(text, limit) -> str:
        try:
            s = str(text or "").replace("\n", " ").strip()
            return s[:limit] if s else "(空)"
        except Exception:
            return "(无法解析)"

    @staticmethod
    def _parse_tool_result(tool_result):
        """兼容 CallToolResult(mcp) / str / 其他对象，返回 (is_error, 文本)"""
        try:
            if tool_result is None:
                return False, "(无返回)"
            if isinstance(tool_result, str):
                return False, tool_result
            if hasattr(tool_result, "content"):
                texts = []
                for c in getattr(tool_result, "content", None) or []:
                    t = getattr(c, "text", None)
                    if t:
                        texts.append(t)
                err = bool(getattr(tool_result, "isError", False))
                return err, " ".join(texts) if texts else "(返回内容不含文本)"
            return False, str(tool_result)
        except Exception as e:
            return False, f"(解析工具结果时异常: {e})"

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        try:
            sender = ""
            try:
                sender = event.get_sender_name() or event.get_sender_id() or ""
            except Exception:
                pass
            self._record(
                event.unified_msg_origin,
                "用户消息",
                f"{sender}: \"{self._truncate(event.message_str, USER_MSG_LIMIT)}\"",
            )
        except Exception as e:
            logger.warning(f"[trace_export] 用户消息监听异常(已忽略): {e}")

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp):
        try:
            role = getattr(resp, "role", "") or ""
            tool_names = getattr(resp, "tools_call_name", None) or []
            text = ""
            try:
                text = resp.completion_text or ""
            except Exception:
                text = ""
            if role == "err":
                self._record(
                    event.unified_msg_origin,
                    "LLM错误",
                    f"本次 LLM 请求出错（底层框架可能中断或重试）: {self._truncate(text, LLM_MSG_LIMIT)}",
                )
            elif tool_names:
                self._record(
                    event.unified_msg_origin,
                    "LLM决策",
                    f"模型决定调用工具: {', '.join(map(str, tool_names))}",
                )
            elif text:
                self._record(
                    event.unified_msg_origin,
                    "LLM输出",
                    f"模型回复: \"{self._truncate(text, LLM_MSG_LIMIT)}\"",
                )
        except Exception as e:
            logger.warning(f"[trace_export] on_llm_response 钩子异常(已忽略): {e}")

    @filter.on_using_llm_tool()
    async def on_using_tool(self, event: AstrMessageEvent, tool, tool_args):
        try:
            name = getattr(tool, "name", "unknown")
            try:
                args_str = (
                    json.dumps(tool_args, ensure_ascii=False, default=str)
                    if tool_args
                    else "(无参数)"
                )
            except Exception:
                args_str = str(tool_args)
            self._record(
                event.unified_msg_origin,
                "工具调用",
                f"执行工具 {name}，参数: {self._truncate(args_str, DETAIL_LIMIT)}",
            )
        except Exception as e:
            logger.warning(f"[trace_export] on_using_llm_tool 钩子异常(已忽略): {e}")

    @filter.on_llm_tool_respond()
    async def on_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        try:
            name = getattr(tool, "name", "unknown")
            is_err, text = self._parse_tool_result(tool_result)
            status = "error" if is_err else "ok:true"
            self._record(
                event.unified_msg_origin,
                "工具返回",
                f"工具 {name} 返回 [{status}]: {self._truncate(text, DETAIL_LIMIT)}",
            )
        except Exception as e:
            logger.warning(f"[trace_export] on_llm_tool_respond 钩子异常(已忽略): {e}")

    def _format_records(self, records: deque) -> str:
        lines = [
            "=" * 40,
            f"黑匣子执行日志导出  {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"记录条数: {len(records)}（最多保留最近 {MAX_RECORDS} 条）",
            "=" * 40,
        ]
        for i, r in enumerate(records, 1):
            lines.append(f"#{i:03d} [{r['time']}] [{r['kind']}] {r['detail']}")
        return "\n".join(lines)

    @filter.command("export_trace", alias={"导出轨迹"})
    async def export_trace(self, event: AstrMessageEvent):
        """导出当前会话最近 100 条执行日志为 TXT 文件（不经过 LLM，零 Token）"""
        file_path = ""
        try:
            umo = event.unified_msg_origin
            records = self.trace_logs.get(umo)
            if not records:
                yield event.plain_result("当前会话暂无黑匣子记录，无可导出内容。")
                return

            content = self._format_records(records)
            file_path = os.path.join(
                tempfile.gettempdir(),
                f"trace_export_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            )
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                yield event.plain_result(f"日志文件写入失败: {e}")
                return

            # NapCat/aiocqhttp 下 File 组件必须带 file:// 协议的 url 参数，否则报"文件消息缺少参数"
            file_url = "file:///" + file_path.replace("\\", "/")
            chain = [
                Comp.Plain(f"🧳 黑匣子日志导出（共 {len(records)} 条）："),
                Comp.File(file=file_path, name=os.path.basename(file_path), url=file_url),
            ]
            await event.send(event.chain_result(chain))
        except Exception as e:
            logger.error(f"[trace_export] 文件发送失败: {e}")
            try:
                records = self.trace_logs.get(event.unified_msg_origin)
                if records:
                    tail = list(records)[-FALLBACK_TEXT_COUNT:]
                    lines = [
                        f"⚠ 文件发送失败（适配器可能不支持 File 组件）: {e}",
                        f"日志已保存至本地: {file_path or '(未知路径)'}",
                        f"以下为最近 {len(tail)} 条记录：",
                    ]
                    for r in tail:
                        lines.append(f"[{r['time']}] [{r['kind']}] {r['detail']}")
                    yield event.plain_result("\n".join(lines))
                else:
                    yield event.plain_result(f"文件发送失败: {e}")
            except Exception as e2:
                logger.error(f"[trace_export] 降级文本发送也失败: {e2}")

    @filter.command("clear_trace", alias={"清空轨迹"})
    async def clear_trace(self, event: AstrMessageEvent):
        """清空当前会话的黑匣子记录"""
        try:
            umo = event.unified_msg_origin
            removed = len(self.trace_logs.get(umo) or [])
            if umo in self.trace_logs:
                self.trace_logs[umo].clear()
            yield event.plain_result(f"🧹 黑匣子已清空（本次清除 {removed} 条记录）")
        except Exception as e:
            logger.error(f"[trace_export] clear_trace 执行失败: {e}")
            try:
                yield event.plain_result(f"清空黑匣子失败: {e}")
            except Exception:
                pass

    async def terminate(self):
        try:
            self.trace_logs.clear()
        except Exception:
            pass

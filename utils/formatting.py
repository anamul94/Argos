"""Telegram message formatting helpers."""

import html
import re

_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def markdown_to_telegram_html(text: str) -> str:
    """Convert Markdown to Telegram-safe HTML (parse_mode='HTML').

    Returns the input string with Markdown syntax replaced by Telegram
    HTML tags. Handles code blocks, inline code, bold, italic, lists,
    headers, and links.
    """
    if not text:
        return text

    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _stash_block(m: re.Match) -> str:
        code_blocks.append(m.group(1).strip())
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    def _stash_inline(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(_stash_block, text)
    text = _INLINE_CODE_RE.sub(_stash_inline, text)
    text = html.escape(text)

    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    # Avoid turning identifiers like reboot_ec2_instance into italics.
    text = re.sub(r"(?<![\w_])_(?!_)(.+?)(?<!_)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[\-\*\+]\s+(.+)$", r"• \1", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", f"<code>{html.escape(code)}</code>")
    for i, code in enumerate(code_blocks):
        text = text.replace(
            f"\x00CODEBLOCK{i}\x00",
            f"<pre><code>{html.escape(code)}</code></pre>",
        )

    return re.sub(r"\n{3,}", "\n\n", text).strip()

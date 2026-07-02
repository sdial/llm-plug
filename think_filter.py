"""
Think content filter module.

Filters out thinking process (in <think>...</think> or similar tags) from model output.
Supports both static and streaming modes.
"""

import re


def filter_think_content_static(content: str) -> str:
    """
    Static filter: remove content between think tags.

    Supports formats:
    - <think>...</think>
    - &#x1F4AD;...&#x1F4AD; (💭 emoji)

    Args:
        content: Original text

    Returns:
        Filtered text with think content removed
    """
    if not content:
        return content

    # Filter <think>...</think>
    result = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

    # Filter 💭...💭 (emoji)
    result = re.sub(r"💭.*?💭", "", result, flags=re.DOTALL)

    return result.strip()


class ThinkFilter:
    """
    Streaming incremental filter for think content.

    Handles think tags that may span multiple chunks.
    """

    def __init__(self):
        self.buffer = ""
        self.in_think = False
        self._think_start_tag = ""

    def feed(self, chunk: str) -> str:
        """
        Process a chunk and return filtered content.

        Args:
            chunk: Input text chunk

        Returns:
            Filtered text (may be empty if chunk is inside think block)
        """
        if not chunk:
            return ""

        self.buffer += chunk
        result_parts = []

        i = 0
        while i < len(self.buffer):
            if self.in_think:
                # Look for end tag
                end_pos = self._find_end_tag(self.buffer, i)
                if end_pos == -1:
                    # End tag not found, keep in buffer
                    self.buffer = self.buffer[i:]
                    return "".join(result_parts)
                # Found end tag, skip past it
                self.in_think = False
                self._think_start_tag = ""
                i = end_pos
            else:
                # Look for start tag
                start_pos = self._find_start_tag(self.buffer, i)
                if start_pos == -1:
                    # No full start tag found. Only keep a tail that could
                    # become a split "<think>" tag in the next chunk.
                    safe_len = len(self.buffer) - self._partial_start_tag_len(
                        self.buffer
                    )
                    if safe_len > i:
                        result_parts.append(self.buffer[i:safe_len])
                        self.buffer = self.buffer[safe_len:]
                    else:
                        # Keep buffer as is
                        pass
                    return "".join(result_parts)
                # Output content before start tag
                if start_pos > i:
                    result_parts.append(self.buffer[i:start_pos])
                start_tag = self._get_start_tag(self.buffer, start_pos)
                self.in_think = True
                self._think_start_tag = start_tag
                i = start_pos + len(start_tag)

        self.buffer = ""
        return "".join(result_parts)

    def _find_start_tag(self, text: str, start: int) -> int:
        """Find start tag position, return -1 if not found."""
        # Check <think>
        idx = text.find("<think>", start)
        if idx != -1:
            return idx
        # Check emoji
        idx = text.find("💭", start)
        if idx != -1:
            return idx
        return -1

    def _partial_start_tag_len(self, text: str) -> int:
        """Return tail length if text ends with a prefix of a start tag."""
        candidates = ("<think>", "💭")
        max_len = 0
        for tag in candidates:
            for length in range(1, len(tag)):
                if text.endswith(tag[:length]):
                    max_len = max(max_len, length)
        return max_len

    def _get_start_tag(self, text: str, pos: int) -> str:
        """Get the start tag at position."""
        if text[pos : pos + 7] == "<think>":
            return "<think>"
        if text[pos : pos + 1] == "💭":
            return "💭"
        return text[pos : pos + 1]

    def _find_end_tag(self, text: str, start: int) -> int:
        """Find position after end tag, return -1 if not found."""
        if self._think_start_tag == "<think>":
            idx = text.find("</think>", start)
            if idx != -1:
                return idx + 8
        elif self._think_start_tag == "💭":
            idx = text.find("💭", start)
            if idx != -1:
                return idx + 1
        return -1

    def flush(self) -> str:
        """
        Return remaining content at end of stream.

        If inside think block, discard. Otherwise return buffer.
        """
        if self.in_think:
            if self._think_start_tag == "💭":
                result = self._think_start_tag + self.buffer
                self.buffer = ""
                self.in_think = False
                self._think_start_tag = ""
                return result
            self.buffer = ""
            self.in_think = False
            self._think_start_tag = ""
            return ""
        result = self.buffer
        self.buffer = ""
        return result

    def reset(self):
        """Reset filter state."""
        self.buffer = ""
        self.in_think = False
        self._think_start_tag = ""

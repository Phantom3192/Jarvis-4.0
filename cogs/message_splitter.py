"""
Message splitting utility for Discord's 2000-character limit.
Intelligently splits long messages while maintaining natural continuity.
"""

DISCORD_MAX_LENGTH = 2000
CONTINUATION_MARKER = "…"  # Indicates message continuation

import re
import discord
from cogs.state import record_mention

_AM_NONE = discord.AllowedMentions.none()


def split_message(text: str, max_length: int = DISCORD_MAX_LENGTH) -> list[str]:
    """
    Split a long message into multiple parts that fit within Discord's character limit.
    
    Strategy:
    1. Always fill each part up to max_length (or very close to it)
    2. Split at sentence boundaries when possible (. ! ?)
    3. If no sentence boundary, split at paragraph/newline boundaries
    4. If still no boundary, force-split at exactly max_length
    5. Add continuation markers to indicate message flow
    
    Args:
        text: The message to split
        max_length: Maximum characters per message (default: 2000)
    
    Returns:
        List of message strings, each within the character limit
    """
    if len(text) <= max_length:
        return [text]
    
    parts = []
    remaining = text
    
    while remaining:
        if len(remaining) <= max_length:
            # Last chunk fits completely
            parts.append(remaining)
            break
        
        # Try to find a good split point within max_length characters
        chunk = remaining[:max_length]
        
        # Strategy 1: Split at sentence boundary (. ! ?)
        for sentence_end in [". ", "! ", "? "]:
            last_pos = chunk.rfind(sentence_end)
            if last_pos > max_length * 0.7:  # Only if it's in the last 30% (not too early)
                split_pos = last_pos + 1
                parts.append(remaining[:split_pos])
                remaining = remaining[split_pos:].lstrip()
                break
        else:
            # Strategy 2: Split at newline/paragraph boundary
            last_newline = chunk.rfind("\n")
            if last_newline > max_length * 0.6:  # Newline in last 40%
                split_pos = last_newline + 1
                parts.append(remaining[:split_pos])
                remaining = remaining[split_pos:].lstrip()
            else:
                # Strategy 3: Split at space boundary (word break)
                last_space = chunk.rfind(" ")
                if last_space > max_length * 0.8:  # Space in last 20%
                    split_pos = last_space
                    parts.append(remaining[:split_pos])
                    remaining = remaining[split_pos:].lstrip()
                else:
                    # Strategy 4: Force split at max_length (last resort)
                    parts.append(chunk)
                    remaining = remaining[max_length:]
    
    return parts


async def send_long_message(
    message_or_interaction,
    text: str,
    max_length: int = DISCORD_MAX_LENGTH,
    ephemeral: bool = False,
) -> list:
    """
    Send a potentially long message to Discord, splitting if necessary.
    Works with both message replies and interaction responses.
    
    Args:
        message_or_interaction: discord.Message or discord.Interaction object
        text: The message content to send
        max_length: Maximum characters per message (default: 2000)
        ephemeral: Whether to make the message ephemeral (interactions only)
    
    Returns:
        List of sent message objects
    """
    parts = split_message(text, max_length)
    sent_messages = []

    # Determine if this is a Message or Interaction
    is_interaction = hasattr(message_or_interaction, "response")

    for i, part in enumerate(parts):
        # Add "continued..." indicator if not the first message
        display_text = part
        if i > 0:
            display_text = f"**[Continued]** {part}"

        # Detect explicit mention tokens like <@123...> in the outgoing text.
        MENTION_RE = re.compile(r"<@!?(?P<id>\d+)>")
        found = {int(m.group('id')) for m in MENTION_RE.finditer(display_text)}
        if found:
            # Determine invoker id (Interaction.user or Message.author)
            invoker = getattr(message_or_interaction, "user", None) or getattr(message_or_interaction, "author", None)
            invoker_id = invoker.id if invoker is not None else None
            if invoker_id is not None:
                for target_id in found:
                    allowed, t = record_mention(invoker_id, target_id)
                    if not allowed:
                        # Inform the invoker and abort sending the reply
                        minutes = int(t / 60) if t >= 60 else None
                        if minutes:
                            timeout_msg = f"⏱️ You have been temporarily blocked from using Jarvis for {minutes} minute(s) due to mention spamming."
                        else:
                            timeout_msg = f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to mention spamming."
                        try:
                            if is_interaction:
                                await message_or_interaction.response.send_message(timeout_msg, ephemeral=True, allowed_mentions=_AM_NONE)
                            else:
                                await message_or_interaction.reply(timeout_msg, allowed_mentions=_AM_NONE)
                        except Exception:
                            pass
                        return []

        # Sanitize common ping tokens to be extra-safe (zero-width space breaks mention)
        display_text = display_text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
        # Break raw mention tokens like <@123> so Discord won't treat them as pings
        display_text = display_text.replace("<@", "<@\u200b")

        try:
            if is_interaction:
                # Interaction: first response uses response.send_message, rest use followup
                if i == 0:
                    await message_or_interaction.response.send_message(
                        display_text,
                        ephemeral=ephemeral,
                        allowed_mentions=_AM_NONE,
                    )
                    # Get the initial response message
                    sent_msg = await message_or_interaction.original_response()
                    sent_messages.append(sent_msg)
                else:
                    sent_msg = await message_or_interaction.followup.send(
                        display_text,
                        ephemeral=ephemeral,
                        allowed_mentions=_AM_NONE,
                    )
                    sent_messages.append(sent_msg)
            else:
                # Message: use reply for all parts
                sent_msg = await message_or_interaction.reply(display_text, allowed_mentions=_AM_NONE)
                sent_messages.append(sent_msg)
        except Exception as e:
            print(f"❌ Error sending message part {i+1}: {e}")

    return sent_messages


async def edit_or_send_long_message(
    interaction,
    text: str,
    max_length: int = DISCORD_MAX_LENGTH,
    ephemeral: bool = False,
) -> list:
    """
    Edit an interaction's original response or send if not already done.
    Handles potential message length overflow by using followups.
    
    Args:
        interaction: discord.Interaction object
        text: The message content
        max_length: Maximum characters per message
        ephemeral: Whether to make messages ephemeral
    
    Returns:
        List of sent/edited message objects
    """
    parts = split_message(text, max_length)
    sent_messages = []
    
    for i, part in enumerate(parts):
        display_text = part
        if i > 0:
            display_text = f"**[Continued]** {part}"
        # Same mention-spam protection for interactions editing/sending responses.
        MENTION_RE = re.compile(r"<@!?(?P<id>\d+)>")
        found = {int(m.group('id')) for m in MENTION_RE.finditer(display_text)}
        if found:
            invoker = getattr(interaction, "user", None)
            invoker_id = invoker.id if invoker is not None else None
            if invoker_id is not None:
                for target_id in found:
                    allowed, t = record_mention(invoker_id, target_id)
                    if not allowed:
                        minutes = int(t / 60) if t >= 60 else None
                        if minutes:
                            timeout_msg = f"⏱️ You have been temporarily blocked from using Jarvis for {minutes} minute(s) due to mention spamming."
                        else:
                            timeout_msg = f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to mention spamming."
                        try:
                            await interaction.response.send_message(timeout_msg, ephemeral=True, allowed_mentions=_AM_NONE)
                        except Exception:
                            pass
                        return []
        
        try:
            if i == 0:
                # Edit or create the initial response
                if interaction.response.is_done():
                    # Already responded, edit it (include allowed_mentions to be safe)
                    try:
                        sent_msg = await interaction.edit_original_response(content=display_text, allowed_mentions=_AM_NONE)
                    except TypeError:
                        # Older versions may not accept allowed_mentions on edit
                        sent_msg = await interaction.edit_original_response(content=display_text)
                else:
                    # Haven't responded yet, send initial response
                    await interaction.response.send_message(display_text, ephemeral=ephemeral, allowed_mentions=_AM_NONE)
                    sent_msg = await interaction.original_response()
                sent_messages.append(sent_msg)
            else:
                # Use followup for continuation
                sent_msg = await interaction.followup.send(display_text, ephemeral=ephemeral, allowed_mentions=_AM_NONE)
                sent_messages.append(sent_msg)
        except Exception as e:
            print(f"❌ Error in edit_or_send_long_message part {i+1}: {e}")
    
    return sent_messages
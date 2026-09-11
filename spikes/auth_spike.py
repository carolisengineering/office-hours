"""Auth spike: does claude-agent-sdk talk to Claude using existing Claude Code
credentials (no ANTHROPIC_API_KEY, no metered billing)?

Run:  uv run python spikes/auth_spike.py
Pass: prints a one-word answer and a tool_use round-trip.
"""

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)


async def main() -> None:
    # 1. plain generation
    opts = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Answer in exactly one word.",
        max_turns=1,
    )
    print("--- plain generation ---")
    async for msg in query(prompt="What color is a ripe banana?", options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    print("text:", block.text.strip())

    # 2. can the model call an in-process tool? (structure we need for final_answer)
    print("--- tool round-trip ---")

    @tool("echo", "Echo the given text back.", {"text": str})
    async def echo(args):
        return {"content": [{"type": "text", "text": f"echo: {args['text']}"}]}

    server = create_sdk_mcp_server(name="spike", version="1.0.0", tools=[echo])
    tool_opts = ClaudeAgentOptions(
        model="haiku",
        max_turns=3,
        mcp_servers={"spike": server},
        allowed_tools=["mcp__spike__echo"],
    )
    async for msg in query(
        prompt="Call the echo tool with text='hello from the spike', then tell me what it returned.",
        options=tool_opts,
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    print("tool_use:", block.name, block.input)
                elif isinstance(block, TextBlock):
                    print("text:", block.text.strip())


if __name__ == "__main__":
    anyio.run(main)

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

import mcp.types as types
from dify_plugin.entities import I18nObject
from dify_plugin.entities.tool import ToolParameter, ToolParameterOption, ToolDescription, ToolProviderType
from dify_plugin.interfaces.agent import ToolEntity, AgentToolIdentity
from mcp import ClientSession
from mcp.client.sse import sse_client


class McpSseClient:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config: dict[str, Any] = config
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()

    async def initialize(self) -> None:
        """Initialize the server connection."""
        try:
            sse_transport = await self.exit_stack.enter_async_context(
                sse_client(url=self.config["url"],
                           headers=self.config["headers"],
                           timeout=self.config["timeout"],
                           sse_read_timeout=self.config["sse_read_timeout"])
            )
            read, write = sse_transport
            session = await self.exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self.session = session
        except Exception as e:
            logging.error(f"Error initializing session: {e}")
            await self.cleanup()
            raise

    async def list_tools(self) -> list[types.Tool]:
        """List available tools from the server.

        Returns:
            A list of available tools.

        Raises:
            RuntimeError: If the server is not initialized.
        """
        if not self.session:
            raise RuntimeError(f"Server '{self.name}' session not initialized")

        result = await self.session.list_tools()
        return result.tools

    async def execute_tool(
            self,
            tool_name: str,
            arguments: dict[str, Any],
            retries: int = 2,
            delay: float = 1.0,
    ) -> types.CallToolResult:
        """Execute a tool with retry mechanism.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.

        Returns:
            Tool execution result.

        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server '{self.name}' session not initialized")

        attempt = 0
        while attempt < retries:
            try:
                logging.info(f"Executing {tool_name}...")
                result = await self.session.call_tool(tool_name, arguments)
                logging.info(result)
                return result
            except Exception as e:
                attempt += 1
                logging.warning(
                    f"Error executing tool: {e}. Attempt {attempt} of {retries}."
                )
                if attempt < retries:
                    logging.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logging.error("Max retries reached. Failing.")
                    raise

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
            except Exception as e:
                logging.error(f"Error during cleanup of server ’{self.name}' session: {e}")


def fetch_mcp_tools(clients: list[McpSseClient]) -> list[ToolEntity]:
    """
    Fetch MCP Servers all tools list by HTTP with SSE transport
    """

    async def fetch_tools():
        all_tools = []
        for client in clients:
            try:
                await client.initialize()
                mcp_tools = await client.list_tools()
            finally:
                await client.cleanup()
            tools = [to_tool_entity(name=client.name, tool=tool) for tool in mcp_tools]
            all_tools.extend(tools)
        return all_tools

    return asyncio.run(fetch_tools())


def to_tool_entity(name: str, tool: types.Tool) -> ToolEntity:
    """
    Convert a MCP Tool to a Dify ToolEntity
    """
    identity = AgentToolIdentity(author="junjiem",
                                 name=tool.name,
                                 label=I18nObject(en_US=tool.name),
                                 provider=name,
                                 )

    parameters = []
    input_schema = tool.inputSchema
    required_params = input_schema.get("required", [])
    properties = input_schema.get("properties", {})
    options = None

    for param_name, param_schema in properties.items():
        type = param_schema.get("type", "string")
        description = param_schema.get("description", None)
        if type == "number":
            param_type = ToolParameter.ToolParameterType.NUMBER
        elif type == "boolean":
            param_type = ToolParameter.ToolParameterType.BOOLEAN
        elif type == "string":
            param_type = ToolParameter.ToolParameterType.STRING
        elif type == "array":
            items = param_schema.get("items")
            if "enum" in items:
                param_type = ToolParameter.ToolParameterType.SELECT
                options = [ToolParameterOption(value=v) for v in items["enum"]]
            elif "type" in items:
                param_type = ToolParameter.ToolParameterType.STRING
                description = f"param_type: array[{items['type']}], {description}"
            else:
                param_type = ToolParameter.ToolParameterType.STRING
        else:
            param_type = ToolParameter.ToolParameterType.STRING

        parameter = ToolParameter(
            name=param_name,
            label=I18nObject(en_US=param_name),
            type=param_type,
            required=param_name in required_params,
            human_description=I18nObject(en_US=description),
            llm_description=description,
            options=options,
            form=ToolParameter.ToolParameterForm.LLM
        )
        parameters.append(parameter)

    tool_description = ToolDescription(human=I18nObject(en_US=tool.description), llm=tool.description) \
        if tool.description else None
    return ToolEntity(
        identity=identity,
        parameters=parameters,
        description=tool_description,
        output_schema=None,
        provider_type=ToolProviderType.API,
        has_runtime_parameters=False,
        runtime_parameters={}
    )


def execute_mcp_tool(clients: list[McpSseClient], tool_name: str, arguments: dict[str, Any]) -> str:
    """
    Execute a MCP Tool
    """

    async def execute_tool():
        for client in clients:
            try:
                await client.initialize()
                tools = await client.list_tools()
            except Exception as e:
                await client.cleanup()
                error_msg = f"Error initializing or list tools: {str(e)}"
                logging.error(error_msg)
                continue
            if any(tool.name == tool_name for tool in tools):
                try:
                    result = await client.execute_tool(tool_name, arguments)
                    if isinstance(result, dict) and "progress" in result:
                        progress = result["progress"]
                        total = result["total"]
                        percentage = (progress / total) * 100
                        logging.info(
                            f"Progress: {progress}/{total} "
                            f"({percentage:.1f}%)"
                        )
                    return f"Tool execution result: {result}"
                except Exception as e:
                    error_msg = f"Error executing tool: {str(e)}"
                    logging.error(error_msg)
                    return error_msg
                finally:
                    await client.cleanup()

    try:
        yield asyncio.run(execute_tool())
    except Exception as e:
        error_msg = f"Error executing tool: {str(e)}"
        logging.error(error_msg)
        yield error_msg

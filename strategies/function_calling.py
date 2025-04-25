import json
import time
import subprocess
from collections.abc import Generator
from copy import deepcopy
from typing import Any, Optional, cast

import pydantic
from dify_plugin.entities.agent import AgentInvokeMessage
from dify_plugin.entities.model import ModelFeature
from dify_plugin.entities.model.llm import (
    LLMModelConfig,
    LLMResult,
    LLMResultChunk,
    LLMUsage,
)
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessage,
    PromptMessageContentType,
    SystemPromptMessage,
    ToolPromptMessage,
    UserPromptMessage,
    PromptMessageTool,
)
from dify_plugin.entities.tool import LogMetadata, ToolInvokeMessage, ToolProviderType
from dify_plugin.interfaces.agent import AgentModelConfig, AgentStrategy, ToolEntity, ToolInvokeMeta
from pydantic import BaseModel

from utils.mcp_client import McpClients


class FunctionCallingParams(BaseModel):
    query: str
    instruction: str | None
    model: AgentModelConfig
    tools: list[ToolEntity] | None
    mcp_servers_config: str | None
    maximum_iterations: int = 3


class FunctionCallingAgentStrategy(AgentStrategy):
    def __init__(self, session):
        super().__init__(session)
        self.query = ""
        self.instruction = ""

    @property
    def _user_prompt_message(self) -> UserPromptMessage:
        return UserPromptMessage(content=self.query)

    @property
    def _system_prompt_message(self) -> SystemPromptMessage:
        return SystemPromptMessage(content=self.instruction)

    def _invoke(self, parameters: dict[str, Any]) -> Generator[AgentInvokeMessage]:
        """
        Run FunctionCall agent application
        """

        try:
            fc_params = FunctionCallingParams(**parameters)
        except pydantic.ValidationError as e:
            raise ValueError(f"Invalid parameters: {e!s}") from e

        # init prompt messages
        query = fc_params.query
        self.query = query
        self.instruction = fc_params.instruction
        history_prompt_messages = fc_params.model.history_prompt_messages
        history_prompt_messages.insert(0, self._system_prompt_message)
        history_prompt_messages.append(self._user_prompt_message)

        # convert tool messages
        tools = fc_params.tools
        tool_instances = {tool.identity.name: tool for tool in tools} if tools else {}

        # Fetch MCP tools
        mcp_clients = None
        mcp_tools = []
        mcp_tool_instances = {}
        servers_config_json = fc_params.mcp_servers_config
        if servers_config_json:
            try:
                config = json.loads(servers_config_json)
                # 支持 mcpServers 包装形式
                servers_config = config.get("mcpServers", config)
            except json.JSONDecodeError as e:
                raise ValueError(f"mcp_servers_config must be a valid JSON string: {e}")
            mcp_clients = McpClients(servers_config)
            mcp_tools = mcp_clients.fetch_tools()
            mcp_tool_instances = {tool.get("name"): tool for tool in mcp_tools} if mcp_tools else {}

        # convert tools into ModelRuntime Tool format
        prompt_messages_tools = self._init_prompt_tools(tools)
        prompt_messages_tools.extend(self._init_prompt_mcp_tools(mcp_tools))

        # init model parameters
        stream = (
            ModelFeature.STREAM_TOOL_CALL in fc_params.model.entity.features
            if fc_params.model.entity and fc_params.model.entity.features
            else False
        )
        model = fc_params.model
        stop = fc_params.model.completion_params.get("stop", []) if fc_params.model.completion_params else []

        # init function calling state
        iteration_step = 1
        max_iteration_steps = fc_params.maximum_iterations
        current_thoughts: list[PromptMessage] = []
        function_call_state = True  # continue to run until there is not any tool call
        llm_usage: dict[str, Optional[LLMUsage]] = {"usage": None}
        final_answer = ""

        while function_call_state and iteration_step <= max_iteration_steps:
            # start a new round
            function_call_state = False
            round_started_at = time.perf_counter()
            round_log = self.create_log_message(
                label=f"ROUND {iteration_step}",
                data={},
                metadata={
                    LogMetadata.STARTED_AT: round_started_at,
                },
                status=ToolInvokeMessage.LogMessage.LogStatus.START,
            )
            yield round_log

            # If max_iteration_steps=1, need to execute tool calls
            if iteration_step == max_iteration_steps and max_iteration_steps > 1:
                # the last iteration, remove all tools
                prompt_messages_tools = []

            # recalc llm max tokens
            prompt_messages = self._organize_prompt_messages(
                history_prompt_messages=history_prompt_messages,
                current_thoughts=current_thoughts,
            )
            if model.entity and model.completion_params:
                self.recalc_llm_max_tokens(model.entity, prompt_messages, model.completion_params)
            # invoke model
            model_started_at = time.perf_counter()
            model_log = self.create_log_message(
                label=f"{model.model} Thought",
                data={},
                metadata={
                    LogMetadata.STARTED_AT: model_started_at,
                    LogMetadata.PROVIDER: model.provider,
                },
                parent=round_log,
                status=ToolInvokeMessage.LogMessage.LogStatus.START,
            )
            yield model_log
            model_config = LLMModelConfig(**model.model_dump(mode="json"))
            chunks: Generator[LLMResultChunk, None, None] | LLMResult = self.session.model.llm.invoke(
                model_config=model_config,
                prompt_messages=prompt_messages,
                stop=stop,
                stream=stream,
                tools=prompt_messages_tools,
            )

            tool_calls: list[tuple[str, str, dict[str, Any]]] = []

            # save full response
            response = ""

            # save tool call names and inputs
            tool_call_names = ""

            current_llm_usage = None

            if isinstance(chunks, Generator):
                for chunk in chunks:
                    # check if there is any tool call
                    if self.check_tool_calls(chunk):
                        function_call_state = True
                        tool_calls.extend(self.extract_tool_calls(chunk) or [])
                        tool_call_names = ";".join([tool_call[1] for tool_call in tool_calls])

                    if chunk.delta.message and chunk.delta.message.content:
                        if isinstance(chunk.delta.message.content, list):
                            for content in chunk.delta.message.content:
                                response += content.data
                                if not function_call_state or iteration_step == max_iteration_steps:
                                    yield self.create_text_message(content.data)
                        else:
                            response += str(chunk.delta.message.content)
                            if not function_call_state or iteration_step == max_iteration_steps:
                                yield self.create_text_message(str(chunk.delta.message.content))

                    if chunk.delta.usage:
                        self.increase_usage(llm_usage, chunk.delta.usage)
                        current_llm_usage = chunk.delta.usage

            else:
                result = chunks
                result = cast(LLMResult, result)
                # check if there is any tool call
                if self.check_blocking_tool_calls(result):
                    function_call_state = True
                    tool_calls.extend(self.extract_blocking_tool_calls(result) or [])
                    tool_call_names = ";".join([tool_call[1] for tool_call in tool_calls])

                if result.usage:
                    self.increase_usage(llm_usage, result.usage)
                    current_llm_usage = result.usage

                if result.message and result.message.content:
                    if isinstance(result.message.content, list):
                        for content in result.message.content:
                            response += content.data
                    else:
                        response += str(result.message.content)

                if not result.message.content:
                    result.message.content = ""
                if isinstance(result.message.content, str):
                    yield self.create_text_message(result.message.content)
                elif isinstance(result.message.content, list):
                    for content in result.message.content:
                        yield self.create_text_message(content.data)

            yield self.finish_log_message(
                log=model_log,
                data={
                    "output": response,
                    "tool_name": tool_call_names,
                    "tool_input": {tool_call[1]: tool_call[2] for tool_call in tool_calls},
                },
                metadata={
                    LogMetadata.STARTED_AT: model_started_at,
                    LogMetadata.FINISHED_AT: time.perf_counter(),
                    LogMetadata.ELAPSED_TIME: time.perf_counter() - model_started_at,
                    LogMetadata.PROVIDER: model.provider,
                    LogMetadata.TOTAL_PRICE: current_llm_usage.total_price if current_llm_usage else 0,
                    LogMetadata.CURRENCY: current_llm_usage.currency if current_llm_usage else "",
                    LogMetadata.TOTAL_TOKENS: current_llm_usage.total_tokens if current_llm_usage else 0,
                },
            )
            assistant_message = AssistantPromptMessage(content="", tool_calls=[])
            if tool_calls:
                assistant_message.tool_calls = [
                    AssistantPromptMessage.ToolCall(
                        id=tool_call[0],
                        type="function",
                        function=AssistantPromptMessage.ToolCall.ToolCallFunction(
                            name=tool_call[1],
                            arguments=json.dumps(tool_call[2], ensure_ascii=False),
                        ),
                    )
                    for tool_call in tool_calls
                ]
            else:
                assistant_message.content = response

            current_thoughts.append(assistant_message)

            final_answer += response + "\n"

            # call tools
            tool_responses = []
            for tool_call_id, tool_call_name, tool_call_args in tool_calls:
                tool_instance = tool_instances.get(tool_call_name)
                tool_provider = tool_instance.identity.provider if tool_instance else ""
                 = s.get(tool_call_name)
                tool_call_started_at = time.perf_counter()
                tool_call_log = self.create_log_message(
                    label=f"CALL {tool_call_name}",
                    data={},
                    metadata={
                        LogMetadata.STARTED_AT: time.perf_counter(),
                        LogMetadata.PROVIDER: tool_provider,
                    },
                    parent=round_log,
                    status=ToolInvokeMessage.LogMessage.LogStatus.START,
                )
                yield tool_call_log
                if not tool_instance and not :
                    tool_response = {
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_call_name,
                        "tool_response": f"there is not a tool named {tool_call_name}",
                        "meta": ToolInvokeMeta.error_instance(f"there is not a tool named {tool_call_name}").to_dict(),
                    }
                else:

                    tool_invoke_parameters = {}
                    try:
                        if mcp_tool_instance:
                            # invoke MCP tool
                            tool_invoke_parameters = tool_call_args
                            # 检查是否是本地命令执行
                            server_config = servers_config.get(tool_call_name)
                            if server_config and "command" in server_config:
                                # 执行本地命令
                                command = server_config["command"]
                                args = server_config.get("args", [])
                                
                                # 构建完整命令
                                cmd_list = [command] + args
                                
                                # 执行命令并获取输出
                                try:
                                    process = subprocess.run(
                                        cmd_list,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        text=True,
                                        check=True
                                    )
                                    result = process.stdout
                                except subprocess.CalledProcessError as e:
                                    result = f"Command execution failed: {e}\nStderr: {e.stderr}"
                                except Exception as e:
                                    result = f"Failed to execute command: {e}"
                            else:
                                # 常规 MCP 工具执行
                                result = mcp_clients.execute_tool(
                                    tool_name=tool_call_name,
                                    tool_args=tool_invoke_parameters,
                                )
                        else:
                            # invoke tool
                            tool_invoke_parameters = {**tool_instance.runtime_parameters, **tool_call_args}
                            tool_invoke_responses = self.session.tool.invoke(
                                provider_type=ToolProviderType(tool_instance.provider_type),
                                provider=tool_instance.identity.provider,
                                tool_name=tool_instance.identity.name,
                                parameters=tool_invoke_parameters,
                            )
                            result = ""
                            for response in tool_invoke_responses:
                                if response.type == ToolInvokeMessage.MessageType.TEXT:
                                    result += cast(ToolInvokeMessage.TextMessage, response.message).text
                                elif response.type == ToolInvokeMessage.MessageType.LINK:
                                    result += (
                                            f"result link: {cast(ToolInvokeMessage.TextMessage, response.message).text}."
                                            + " please tell user to check it."
                                    )
                                elif response.type in {
                                    ToolInvokeMessage.MessageType.IMAGE_LINK,
                                    ToolInvokeMessage.MessageType.IMAGE,
                                }:
                                    result += (
                                            "image has been created and sent to user already, "
                                            + "you do not need to create it, just tell the user to check it now."
                                    )
                                elif response.type == ToolInvokeMessage.MessageType.JSON:
                                    text = json.dumps(
                                        cast(ToolInvokeMessage.JsonMessage, response.message).json_object,
                                        ensure_ascii=False,
                                    )
                                    result += f"tool response: {text}."
                                else:
                                    result += f"tool response: {response.message!r}."
                    except Exception as e:
                        result = f"tool invoke error: {e!s}"
                    tool_response = {
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_call_name,
                        "tool_call_input": tool_invoke_parameters,
                        "tool_response": result,
                    }

                yield self.finish_log_message(
                    log=tool_call_log,
                    data={
                        "output": tool_response,
                    },
                    metadata={
                        LogMetadata.STARTED_AT: tool_call_started_at,
                        LogMetadata.PROVIDER: tool_provider,
                        LogMetadata.FINISHED_AT: time.perf_counter(),
                        LogMetadata.ELAPSED_TIME: time.perf_counter() - tool_call_started_at,
                    },
                )
                tool_responses.append(tool_response)
                if tool_response["tool_response"] is not None:
                    current_thoughts.append(
                        ToolPromptMessage(
                            content=str(tool_response["tool_response"]),
                            tool_call_id=tool_call_id,
                            name=tool_call_name,
                        )
                    )

            # update prompt tool
            for prompt_tool in prompt_messages_tools:
                if prompt_tool.name in tool_instances:
                    self.update_prompt_message_tool(tool_instances[prompt_tool.name], prompt_tool)
            yield self.finish_log_message(
                log=round_log,
                data={
                    "output": {
                        "llm_response": response,
                        "tool_responses": tool_responses,
                    },
                },
                metadata={
                    LogMetadata.STARTED_AT: round_started_at,
                    LogMetadata.FINISHED_AT: time.perf_counter(),
                    LogMetadata.ELAPSED_TIME: time.perf_counter() - round_started_at,
                    LogMetadata.TOTAL_PRICE: current_llm_usage.total_price if current_llm_usage else 0,
                    LogMetadata.CURRENCY: current_llm_usage.currency if current_llm_usage else "",
                    LogMetadata.TOTAL_TOKENS: current_llm_usage.total_tokens if current_llm_usage else 0,
                },
            )
            iteration_step += 1

        # All MCP Client close
        if mcp_clients:
            mcp_clients.close()

        yield self.create_json_message(
            {
                "execution_metadata": {
                    LogMetadata.TOTAL_PRICE: llm_usage["usage"].total_price if llm_usage["usage"] is not None else 0,
                    LogMetadata.CURRENCY: llm_usage["usage"].currency if llm_usage["usage"] is not None else "",
                    LogMetadata.TOTAL_TOKENS: llm_usage["usage"].total_tokens if llm_usage["usage"] is not None else 0,
                }
            }
        )

    def check_tool_calls(self, llm_result_chunk: LLMResultChunk) -> bool:
        """
        Check if there is any tool call in llm result chunk
        """
        return bool(llm_result_chunk.delta.message.tool_calls)

    def check_blocking_tool_calls(self, llm_result: LLMResult) -> bool:
        """
        Check if there is any blocking tool call in llm result
        """
        return bool(llm_result.message.tool_calls)

    def extract_tool_calls(self, llm_result_chunk: LLMResultChunk) -> list[tuple[str, str, dict[str, Any]]]:
        """
        Extract tool calls from llm result chunk

        Returns:
            List[Tuple[str, str, Dict[str, Any]]]: [(tool_call_id, tool_call_name, tool_call_args)]
        """
        tool_calls = []
        for prompt_message in llm_result_chunk.delta.message.tool_calls:
            args = {}
            if prompt_message.function.arguments != "":
                args = json.loads(prompt_message.function.arguments)

            tool_calls.append(
                (
                    prompt_message.id,
                    prompt_message.function.name,
                    args,
                )
            )

        return tool_calls

    def extract_blocking_tool_calls(self, llm_result: LLMResult) -> list[tuple[str, str, dict[str, Any]]]:
        """
        Extract blocking tool calls from llm result

        Returns:
            List[Tuple[str, str, Dict[str, Any]]]: [(tool_call_id, tool_call_name, tool_call_args)]
        """
        tool_calls = []
        for prompt_message in llm_result.message.tool_calls:
            args = {}
            if prompt_message.function.arguments != "":
                args = json.loads(prompt_message.function.arguments)

            tool_calls.append(
                (
                    prompt_message.id,
                    prompt_message.function.name,
                    args,
                )
            )

        return tool_calls

    def _init_system_message(self, prompt_template: str, prompt_messages: list[PromptMessage]) -> list[PromptMessage]:
        """
        Initialize system message
        """
        if not prompt_messages and prompt_template:
            return [
                SystemPromptMessage(content=prompt_template),
            ]

        if prompt_messages and not isinstance(prompt_messages[0], SystemPromptMessage) and prompt_template:
            prompt_messages.insert(0, SystemPromptMessage(content=prompt_template))

        return prompt_messages or []

    def _clear_user_prompt_image_messages(self, prompt_messages: list[PromptMessage]) -> list[PromptMessage]:
        """
        As for now, gpt supports both fc and vision at the first iteration.
        We need to remove the image messages from the prompt messages at the first iteration.
        """
        prompt_messages = deepcopy(prompt_messages)

        for prompt_message in prompt_messages:
            if isinstance(prompt_message, UserPromptMessage) and isinstance(prompt_message.content, list):
                prompt_message.content = "\n".join(
                    [
                        content.data
                        if content.type == PromptMessageContentType.TEXT
                        else "[image]"
                        if content.type == PromptMessageContentType.IMAGE
                        else "[file]"
                        for content in prompt_message.content
                    ]
                )

        return prompt_messages

    def _organize_prompt_messages(
            self,
            current_thoughts: list[PromptMessage],
            history_prompt_messages: list[PromptMessage],
    ) -> list[PromptMessage]:
        prompt_messages = [
            *history_prompt_messages,
            *current_thoughts,
        ]
        if len(current_thoughts) != 0:
            # clear messages after the first iteration
            prompt_messages = self._clear_user_prompt_image_messages(prompt_messages)
        return prompt_messages

    @staticmethod
    def _init_prompt_mcp_tools(mcp_tools: list[dict]) -> list[PromptMessageTool]:
        """
        Initialize prompt message MCP tools
        """
        prompt_messages_tools = []

        for tool in mcp_tools:
            parameters = tool.get("inputSchema")
            if "properties" not in parameters:
                parameters["properties"] = {}
            if "required" not in parameters:
                parameters["required"] = []
            prompt_message = PromptMessageTool(
                name=tool.get("name"),
                description=tool.get("description", ""),
                parameters=parameters,
            )
            prompt_messages_tools.append(prompt_message)

        return prompt_messages_tools

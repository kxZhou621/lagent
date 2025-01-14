import copy
import warnings
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Union

from lagent.agents.aggregator import DefaultAggregator
from lagent.hooks import Hook, RemovableHandle
from lagent.llms import BaseLLM
from lagent.memory import Memory, MemoryManager
from lagent.prompts.parsers import StrParser
from lagent.prompts.prompt_template import PromptTemplate
from lagent.schema import AgentMessage
from lagent.utils import create_object


class Agent:
    """Agent is the basic unit of the system. It is responsible for
    communicating with the LLM, managing the memory, and handling the
    message aggregation and parsing. It can also be extended with hooks

    Args:
        llm (Union[BaseLLM, Dict]): The language model used by the agent.
        template (Union[PromptTemplate, str]): The template used to format the
            messages.
        memory (Dict): The memory used by the agent.
        output_format (Dict): The output format used by the agent.
        aggregator (Dict): The aggregator used by the agent.
        name (Optional[str]): The name of the agent.
        description (Optional[str]): The description of the agent.
        hooks (Optional[Union[List[Dict], Dict]]): The hooks used by the agent.

    Returns:
        AgentMessage: The response message.
    """

    def __init__(
        self,
        llm: Union[BaseLLM, Dict] = None,
        template: Union[PromptTemplate, str, dict, List[dict]] = None,
        memory: Dict = dict(type=Memory),
        output_format: Optional[Dict] = None,
        aggregator: Dict = dict(type=DefaultAggregator),
        name: Optional[str] = None,
        description: Optional[str] = None,
        hooks: Optional[Union[List[Dict], Dict]] = None,
    ):
        self.name = name or self.__class__.__name__
        self.llm: BaseLLM = create_object(llm)

        self.memory: MemoryManager = MemoryManager(memory) if memory else None
        self.output_format: StrParser = create_object(output_format)
        self.template = template
        self.description = description
        self.aggregator: DefaultAggregator = create_object(aggregator)
        self._hooks: Dict[int, Hook] = OrderedDict()
        if hooks:
            for hook in hooks:
                hook = create_object(hook)
                self.register_hook(hook)

    def update_memory(self, message, session_id=0):
        if self.memory:
            self.memory.add(message, session_id=session_id)

    def __call__(
        self,
        *message: Union[str, AgentMessage, List[AgentMessage]],
        session_id=0,
        **kwargs,
    ) -> AgentMessage:
        # message.receiver = self.name
        message = [
            AgentMessage(sender=self.name, content=m)
            if isinstance(m, str) else m for m in message
        ]
        for hook in self._hooks.values():
            message = copy.deepcopy(message)
            result = hook.before_agent(self, message, session_id)
            if result:
                message = result

        self.update_memory(message, session_id=session_id)
        response_message = self.forward(
            *message, session_id=session_id, **kwargs)
        if not isinstance(response_message, AgentMessage):
            response_message = AgentMessage(
                sender=self.name,
                content=response_message,
            )
        self.update_memory(response_message, session_id=session_id)
        for hook in self._hooks.values():
            response_message = copy.deepcopy(response_message)
            result = hook.after_agent(self, response_message, session_id)
            if result:
                response_message = result
        return response_message

    def forward(self,
                *message: AgentMessage,
                session_id=0,
                **kwargs) -> Union[AgentMessage, str]:

        formatted_messages = self.aggregator.aggregate(
            self.memory.get(session_id),
            self.name,
            self.output_format,
            self.template,
        )
        llm_response = self.llm.chat(formatted_messages, **kwargs)
        if self.output_format:
            formatted_messages = self.output_format.parse_response(
                llm_response)
            return AgentMessage(
                sender=self.name,
                content=llm_response,
                formatted=formatted_messages,
            )
        return llm_response

    def __setattr__(self, __name: str, __value: Any) -> None:
        if isinstance(__value, Agent):
            _agents = getattr(self, '_agents', OrderedDict())
            _agents[__name] = __value
            super().__setattr__('_agents', _agents)
        super().__setattr__(__name, __value)

    def state_dict(self, session_id=0):
        state_dict, stack = {}, [('', self)]
        while stack:
            prefix, node = stack.pop()
            key = prefix + 'memory'
            if session_id not in node.memory.memory_map:
                raise KeyError(f'No session id {session_id} in {key}')
            state_dict[key] = (
                node.memory.get(session_id).save() if node.memory else None)
            if hasattr(node, '_agents'):
                for name, value in reversed(node._agents.items()):
                    stack.append((prefix + name + '.', value))
        return state_dict

    def load_state_dict(self, state_dict: Dict, session_id=0):
        _state_dict = self.state_dict()
        missing_keys = set(_state_dict) - set(state_dict)
        if missing_keys:
            raise KeyError(f'Missing keys: {missing_keys}')
        extra_keys = set(state_dict) - set(_state_dict)
        if extra_keys:
            warnings.warn(f'Mismatch keys which are not used: {extra_keys}')
        for key in _state_dict:
            obj = self
            for attr in key.split('.')[:-1]:
                obj = getattr(obj, attr)
            if session_id not in obj.memory.memory_map:
                obj.memory.create_instance(session_id)
            obj.memory.memory_map[session_id].load(state_dict[key])

    def register_hook(self, hook: Callable):
        handle = RemovableHandle(self._hooks)
        self._hooks[handle.id] = hook
        return handle

    def reset(self, session_id=0):
        if self.memory:
            self.memory.reset(session_id=session_id)


class AsyncAgent(Agent):

    async def __call__(self,
                       *message: AgentMessage | List[AgentMessage],
                       session_id=0,
                       **kwargs) -> AgentMessage:
        message = [
            AgentMessage(sender=self.name, content=m)
            if isinstance(m, str) else m for m in message
        ]
        for hook in self._hooks.values():
            message = copy.deepcopy(message)
            result = hook.before_agent(self, message, session_id)
            if result:
                message = result

        self.update_memory(message, session_id=session_id)
        response_message = await self.forward(
            *message, session_id=session_id, **kwargs)
        if not isinstance(response_message, AgentMessage):
            response_message = AgentMessage(
                sender=self.name,
                content=response_message,
            )
        self.update_memory(response_message, session_id=session_id)
        for hook in self._hooks.values():
            response_message = copy.deepcopy(response_message)
            result = hook.after_agent(self, response_message, session_id)
            if result:
                response_message = result
        return response_message

    async def forward(self,
                      *message: AgentMessage,
                      session_id=0,
                      **kwargs) -> Union[AgentMessage, str]:

        formatted_messages = self.aggregator.aggregate(
            self.memory.get(session_id),
            self.name,
            self.output_format,
            self.template,
        )
        llm_response = await self.llm.chat(formatted_messages, session_id,
                                           **kwargs)
        if self.output_format:
            formatted_messages = self.output_format.parse_response(
                llm_response)
            return AgentMessage(
                sender=self.name,
                content=llm_response,
                formatted=formatted_messages,
            )
        return llm_response

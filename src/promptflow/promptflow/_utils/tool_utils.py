# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

import importlib
import inspect
import logging
import re
from enum import Enum, EnumMeta
from typing import Any, Callable, Dict, List, Union, get_args, get_origin

from jinja2 import Environment, meta

from promptflow._utils.utils import is_json_serializable
from promptflow.exceptions import ErrorTarget, UserErrorException

from ..contracts.tool import ConnectionType, InputDefinition, Tool, ValueType
from ..contracts.types import PromptTemplate

module_logger = logging.getLogger(__name__)


def value_to_str(val):
    if val is inspect.Parameter.empty:
        # For empty case, default field will be skipped when dumping to json
        return None
    if val is None:
        # Dump default: "" in json to avoid UI validation error
        return ""
    if isinstance(val, Enum):
        return val.value
    return str(val)


def resolve_annotation(anno) -> Union[str, list]:
    """Resolve the union annotation to type list."""
    origin = get_origin(anno)
    if origin != Union:
        return anno
    # Optional[Type] is Union[Type, NoneType], filter NoneType out
    args = [arg for arg in get_args(anno) if arg != type(None)]  # noqa: E721
    return args[0] if len(args) == 1 else args


def param_to_definition(param, gen_custom_type_conn=False) -> (InputDefinition, bool):
    default_value = param.default
    # Get value type and enum from annotation
    value_type = resolve_annotation(param.annotation)
    enum = None
    custom_type_conn = None
    # Get value type and enum from default if no annotation
    if default_value is not inspect.Parameter.empty and value_type == inspect.Parameter.empty:
        value_type = default_value.__class__ if isinstance(default_value, Enum) else type(default_value)
    # Extract enum for enum class
    if isinstance(value_type, EnumMeta):
        enum = [str(option.value) for option in value_type]
        value_type = str
    is_connection = False
    if ConnectionType.is_connection_value(value_type):
        if ConnectionType.is_custom_strong_type(value_type):
            typ = ["CustomConnection"]
            custom_type_conn = [value_type.__name__]
        else:
            typ = [value_type.__name__]
        is_connection = True
    elif isinstance(value_type, list):
        if not all(ConnectionType.is_connection_value(t) for t in value_type):
            typ = [ValueType.OBJECT]
        else:
            custom_connection_added = False
            typ = []
            custom_type_conn = []
            for t in value_type:
                # Add 'CustomConnection' to typ list when custom strong type connection exists. Collect all custom types
                if ConnectionType.is_custom_strong_type(t):
                    if not custom_connection_added:
                        custom_connection_added = True
                        typ.append("CustomConnection")
                    custom_type_conn.append(t.__name__)
                else:
                    if t.__name__ != "CustomConnection":
                        typ.append(t.__name__)
                    elif not custom_connection_added:
                        custom_connection_added = True
                        typ.append(t.__name__)
            is_connection = True
    else:
        typ = [ValueType.from_type(value_type)]

    # 1. Do not generate custom type when generating flow.tools.json for script tool.
    #    Extension would show custom type if it exists. While for script tool with custom strong type connection,
    #    we still want to show 'CustomConnection' type.
    # 2. Generate custom connection type when resolving tool in _tool_resolver, since we rely on it to convert the
    #    custom connection to custom strong type connection.
    if not gen_custom_type_conn:
        custom_type_conn = None

    return (
        InputDefinition(
            type=typ,
            default=value_to_str(default_value),
            description=None,
            enum=enum,
            custom_type=custom_type_conn,
        ),
        is_connection,
    )


def function_to_interface(f: Callable, initialize_inputs=None, gen_custom_type_conn=False) -> tuple:
    sign = inspect.signature(f)
    all_inputs = {}
    input_defs = {}
    connection_types = []
    # Collect all inputs from class and func
    if initialize_inputs:
        if any(k for k in initialize_inputs if k in sign.parameters):
            raise Exception(f'Duplicate inputs found from {f.__name__!r} and "__init__()"!')
        all_inputs = {**initialize_inputs}
    all_inputs.update(
        {
            k: v
            for k, v in sign.parameters.items()
            if k != "self" and v.kind != v.VAR_KEYWORD and v.kind != v.VAR_POSITIONAL  # TODO: Handle these cases
        }
    )
    # Resolve inputs to definitions.
    for k, v in all_inputs.items():
        input_def, is_connection = param_to_definition(v, gen_custom_type_conn=gen_custom_type_conn)
        input_defs[k] = input_def
        if is_connection:
            connection_types.append(input_def.type)
    outputs = {}
    # Note: We don't have output definition now
    return input_defs, outputs, connection_types


def function_to_tool_definition(f: Callable, type=None, initialize_inputs=None) -> Tool:
    """Translate a function to tool definition.

    :param f: Function to be translated.
    :param type: Tool type
    :param initialize_inputs: The initialize() func inputs get by get_initialize_inputs() when function
        defined in class. We will merge those inputs with f() inputs.
    :return: The tool definition.
    """
    if hasattr(f, "__original_function"):
        f = f.__original_function
    inputs, outputs, _ = function_to_interface(f, initialize_inputs)
    # Hack to get class name
    class_name = None
    if "." in f.__qualname__:
        class_name = f.__qualname__.replace(f".{f.__name__}", "")
    meta_dict = {
        "name": f.__qualname__,
        "description": inspect.getdoc(f) or None,
        "inputs": inputs,
        "outputs": outputs,
        "class_name": class_name,
        "function": f.__name__,
    }
    return Tool(type=type, module=f.__module__, **meta_dict, is_builtin=True, stage="test")


def get_inputs_for_prompt_template(template_str):
    """Get all input variable names and definitions from a jinja2 template string.

    : param template_str: template string
    : type t: str
    : return: the input name to InputDefinition dict
    : rtype t: Dict[str, ~promptflow.contracts.tool.InputDefinition]
    Example:
    >>> get_inputs_for_prompt_template(
        template_str="A simple prompt with no variables"
    )
    {}

    >>> get_inputs_for_prompt_template(
        template_str="Prompt with only one string input {{str_input}}"
    )
    {"str_input": InputDefinition(type=[ValueType.STRING])}

    >>> get_inputs_for_prompt_template(
        template_str="Prompt with image input ![image]({{image_input}}) and string input {{str_input}}"
    )
    {"image_input": InputDefinition(type=[ValueType.IMAGE]), "str_input": InputDefinition(type=[ValueType.STRING])
    """
    env = Environment()
    template = env.parse(template_str)
    inputs = sorted(meta.find_undeclared_variables(template), key=lambda x: template_str.find(x))
    result_dict = {i: InputDefinition(type=[ValueType.STRING]) for i in inputs}

    # currently we only support image type
    pattern = r"\!\[(\s*image\s*)\]\(\{\{\s*([^{}]+)\s*\}\}\)"
    matches = re.finditer(pattern, template_str)

    for match in matches:
        input_name = match.group(2).strip()
        result_dict[input_name] = InputDefinition([ValueType(match.group(1).strip())])

    return result_dict


def get_prompt_param_name_from_func(f):
    """Get the param name of prompt template on provider."""
    return next((k for k, annotation in f.__annotations__.items() if annotation == PromptTemplate), None)


def validate_dynamic_list_func_response_type(response: Any, f: str):
    """Verify response type is correct.

    The response is a list of items. Each item is a dict with the following keys:
        - value: for backend use. Required.
        - display_value: for UI display. Optional.
        - hyperlink: external link. Optional.
        - description: information icon tip. Optional.
    The response can not be empty.
    """
    if not response:
        raise ListFunctionResponseError(f"{f} response can not be empty.")
    if not isinstance(response, List):
        raise ListFunctionResponseError(f"{f} response must be a list.")
    for item in response:
        if not isinstance(item, Dict):
            raise ListFunctionResponseError(f"{f} response must be a list of dict. {item} is not a dict.")
        if "value" not in item:
            raise ListFunctionResponseError(f"{f} response dict must have 'value' key.")
        for key, value in item.items():
            if not isinstance(key, str):
                raise ListFunctionResponseError(f"{f} response dict key must be a string. {key} is not a string.")
            if not is_json_serializable(value):
                raise ListFunctionResponseError(f"{f} response dict value {value} is not json serializable.")
            if not isinstance(value, (str, int, float, list, Dict)):
                raise ListFunctionResponseError(
                    f"{f} response dict value must be a string, int, float, list or dict. {value} is not supported."
                )


def append_workspace_triple_to_func_input_params(
    func_sig_params: Dict, func_input_params_dict: Dict, ws_triple_dict: Dict[str, str]
):
    """Append workspace triple to func input params.

    :param func_sig_params: function signature parameters, full params.
    :param func_input_params_dict: user input param key-values for dynamic list function.
    :param ws_triple_dict: workspace triple dict, including subscription_id, resource_group_name, workspace_name.
    :return: combined func input params.
    """
    # append workspace triple to func input params if any below condition are met:
    # 1. func signature has kwargs param.
    # 2. func signature has param named 'subscription_id','resource_group_name','workspace_name'.
    ws_triple_dict = ws_triple_dict if ws_triple_dict is not None else {}
    func_input_params_dict = func_input_params_dict if func_input_params_dict is not None else {}
    has_kwargs_param = any([param.kind == inspect.Parameter.VAR_KEYWORD for _, param in func_sig_params.items()])
    if has_kwargs_param is False:
        # keep only params that are in func signature. Or run into error when calling func.
        avail_ws_info_dict = {k: v for k, v in ws_triple_dict.items() if k in set(func_sig_params.keys())}
    else:
        avail_ws_info_dict = ws_triple_dict

    # if ws triple key is in func input params, it means user has provided value for it,
    # do not expect implicit override.
    combined_func_input_params = dict(avail_ws_info_dict, **func_input_params_dict)
    return combined_func_input_params


def load_function_from_function_path(func_path: str):
    """Load a function from a function path.

    The function path should be in the format of "module_name.function_name".
    """
    try:
        module_name, func_name = func_path.rsplit(".", 1)
        module = importlib.import_module(module_name)
        f = getattr(module, func_name)
        if callable(f):
            return f
        else:
            raise FunctionPathValidationError(f"'{f}' is not callable.")
    except Exception as e:
        raise FunctionPathValidationError(
            f"Failed to parse function from function path: '{func_path}'. Expected format: format 'my_module.my_func'. "
            f"Detailed error: {e}"
        )


class DynamicListError(UserErrorException):
    """Base exception raised for dynamic list errors."""

    def __init__(self, message):
        msg = (
            f"Unable to display list of items due to '{message}'. \nPlease contact the tool author/support team "
            f"for troubleshooting assistance."
        )
        super().__init__(msg, target=ErrorTarget.FUNCTION_PATH)


class ListFunctionResponseError(DynamicListError):
    pass


class FunctionPathValidationError(DynamicListError):
    pass

"""Verify an actual chat -> native call -> tool result round trip, under budget."""
import json
import secrets

from .analysis import Budget, BudgetExceeded, ModelFailure
from .model import ChatModel
from .collectors import _now


def probe(client, config, db_path):
    budget = Budget(db_path, config)
    model = ChatModel(client, config, budget)
    stages = []
    stage = '网络、认证与基本响应'
    try:
        reply = model.complete([{'role': 'user', 'content': 'Return JSON {"ok":true} to test this connection.'}], [])
        if not reply.get('content'):
            raise ModelFailure('接口未返回基本文本响应')
        stages.append({'name': stage, 'ok': True, 'message': '实际请求成功并获得文本；不代表已支持工具调用。'})
        stage = '原生 Function Calling 与参数校验'
        marker = secrets.token_hex(6)
        schema = {'type': 'function', 'function': {'name': 'finish_research', 'description': 'Connection probe only',
            'parameters': {'type': 'object', 'properties': {'marker': {'type': 'string', 'enum': [marker]}},
                           'required': ['marker'], 'additionalProperties': False}}}
        messages = [{'role': 'user', 'content': 'Call finish_research with marker ' + marker}]
        reply = model.complete(messages, [schema], finish_only=True)
        calls = reply.get('tool_calls', [])
        if len(calls) != 1 or calls[0]['function']['name'] != 'finish_research' or json.loads(calls[0]['function']['arguments']) != {'marker': marker}:
            raise ModelFailure('模型未返回所要求的原生工具调用和正确 JSON 参数')
        stages.append({'name': stage, 'ok': True, 'message': '已收到 tool_calls，工具名称和参数均通过检查。'})
        stage = '工具观察回传与结构化结果'
        # New nonce is present only in the tool observation: model must consume it.
        observed = secrets.token_hex(8)
        messages += [reply, {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': json.dumps({'observed': observed})},
                     {'role': 'user', 'content': 'Return only a JSON object with the observed string from the tool: {"observed":"..."}.'}]
        final = model.complete(messages, [])
        if json.loads(final.get('content') or '') != {'observed': observed}:
            raise ModelFailure('模型没有正确读取工具结果或未返回要求的 JSON')
        stages.append({'name': stage, 'ok': True, 'message': '工具观察往返和 JSON 结果校验通过。'})
    except (BudgetExceeded, ModelFailure, ValueError, TypeError) as exc:
        stages.append({'name': stage, 'ok': False, 'message': str(exc) if isinstance(exc, (BudgetExceeded, ModelFailure)) else '模型返回内容不符合测试结构'})
    return {'checked_at': _now(), 'ok': len(stages) == 3 and all(x['ok'] for x in stages), 'stages': stages, 'usage': budget.usage()}

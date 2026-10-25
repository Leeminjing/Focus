"""Owned real Gateway process with a scripted provider and graceful test stop.

All composition, policy, database, dispatch, browser tools and native host are
production code. Only model sampling is deterministic. Never grants permissions.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sys
import threading

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from focus.models.responses import FocusResponsesChatModel
from backend.app.gateway.app import app
import uvicorn

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def content(message):
    if isinstance(message.content, str): return message.content
    return next((item.get('text', '') for item in message.content if isinstance(item, dict) and item.get('type') == 'text'), '')


async def stream(model, messages, **kwargs):
    commands = [re.search(r'RECOVERY_CASE=(\w+)', content(message)) for message in messages if isinstance(message, HumanMessage)]
    case = next(match.group(1) for match in reversed(commands) if match)
    prefix = 'recovery-' + case + '-'
    outputs = [message for message in messages if isinstance(message, ToolMessage) and str(message.tool_call_id).startswith(prefix)]
    views = [json.loads(content(message)) for message in outputs if message.name == 'browser_observe' and message.status != 'error']
    identity = views[-1]['observation_id'] if views else 'missing-view'
    fields = {'observation_id': identity, 'coordinate_space': 'css_viewport'}
    base = os.environ['FOCUS_RECOVERY_SITE']
    if case.endswith('_HOLD'):
        steps = [('browser_observe', {}), ('browser_navigate', {'observation_id': identity, 'url': base + '/slow-HOLD'})]
    elif case.endswith('_RESUME'):
        steps = [('browser_observe', {}), ('browser_release', {})]
    else:
        name = 'A' if case.endswith('_A') else 'B'
        steps = [('browser_observe', {}), ('browser_navigate', {'observation_id': identity, 'url': base + '/' + name}),
                 ('browser_observe', {}), ('browser_click', {**fields, 'x': 80, 'y': 40}),
                 ('browser_type', {'observation_id': identity, 'text': 'persist ' + name})]
        if name == 'A':
            steps += [('browser_observe', {}), ('browser_click', {**fields, 'x': 80, 'y': 220}),
                      ('browser_list_pages', {}), ('browser_observe', {}),
                      ('browser_click', {**fields, 'x': 80, 'y': 40}),
                      ('browser_type', {'observation_id': identity, 'text': 'persist child'})]
        steps += [('browser_release', {})]
    audit = {'case': case, 'completed_tools': [message.name for message in outputs],
             'observations': [view['observation_id'] for view in views]}
    with Path(os.environ['FOCUS_RECOVERY_AUDIT']).open('a', encoding='utf-8') as output:
        output.write(json.dumps(audit) + '\n')
    if len(outputs) < len(steps):
        name, arguments = steps[len(outputs)]
        if name == 'browser_observe': await asyncio.sleep(.15)
        yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_call_chunks=[{
            'name': name, 'args': json.dumps(arguments), 'id': prefix + str(len(outputs)), 'index': 0}]))
    else:
        yield ChatGenerationChunk(message=AIMessageChunk(content='Recovery workflow inspected without replay.'))


FocusResponsesChatModel._astream = stream
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application) as state:
        model = application.state.desktop_service.app_config.get_model('deepseek-v4-flash')
        model.use, model.provider, model.protocol = 'focus.models.responses:FocusResponsesChatModel', 'deepseek', 'responses'
        model.supports_image_input = True
        yield state


app.router.lifespan_context = lifespan

server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='info',
    loop='backend.app.gateway.app:selector_loop_factory'))


def stop():
    for line in sys.stdin:
        if line.strip() == 'quit':
            server.should_exit = True
            return


threading.Thread(target=stop, daemon=True).start()
server.run()

"""R05：不能在摘要拒绝后绕行静默删除任务要求/批准范围。"""
from __future__ import annotations
import copy
import pytest
from CharTyr_MaiWork.maiwork.compaction import chat_with_retry_on_long_context
from CharTyr_MaiWork.maiwork.models import ModelError, ChatResult

@pytest.mark.asyncio
async def test_context_error_never_retries_with_dropped_requirements():
    history=[{'role':'system','content':'工作规矩'},
             {'role':'user','content':'不要发布到公网；只允许写本任务文件。'},
             {'role':'assistant','content':'记住限制，继续工作。'},
             {'role':'user','content':'下一步'}]
    original=copy.deepcopy(history)
    trimmed=[]
    class M:
        def __init__(self): self.calls=[]
        async def chat(self,role=None,messages=None,**kw):
            self.calls.append(copy.deepcopy(messages))
            if len(self.calls)==1: raise ModelError('context_length_exceeded',status=400)
            return ChatResult(text='假的成功',tool_calls=[],model='m',prompt_tokens=1,completion_tokens=1,raw_message={})
    m=M()
    with pytest.raises(ModelError,match='未丢弃|没有丢弃'):
        await chat_with_retry_on_long_context(history,models=m,role='main',on_trim=lambda ms:trimmed.append(ms))
    assert history==original and m.calls==[original] and trimmed==[]

@pytest.mark.asyncio
async def test_other_error_is_unchanged():
    error=ModelError('授权失败',status=401)
    class M:
        async def chat(self,*a,**k): raise error
    with pytest.raises(ModelError) as raised:
        await chat_with_retry_on_long_context([{'role':'user','content':'hi'}],models=M(),role='worker')
    assert raised.value is error

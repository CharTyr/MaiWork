"""R05：某一分段/最终摘要为空时不允许用占位符替掉原历史。"""
from types import SimpleNamespace
import pytest
from CharTyr_MaiWork.maiwork import compaction
from CharTyr_MaiWork.maiwork.models import ModelError

@pytest.mark.asyncio
@pytest.mark.parametrize('bad_call',[1,2,3])
async def test_empty_piece_or_merge_rejects_compaction(bad_call):
    class M:
        def __init__(self):self.calls=0
        async def chat(self,*a,**kw):
            self.calls+=1
            return SimpleNamespace(text='' if self.calls==bad_call else '重要约束：禁止发布',tool_calls=[])
    models=M()
    messages=[{'role':'user','content':'x'*48000+'不要发布到公网'}]
    with pytest.raises(ModelError,match='空|未完成'):
        await compaction.summarize_messages(messages,models=models,role='main',purpose='test')
    assert models.calls==bad_call

@pytest.mark.asyncio
async def test_empty_summary_never_replaces_original_history():
    class M:
        async def chat(self,*a,**kw):return SimpleNamespace(text=' ',tool_calls=[])
    messages=[{'role':'system','content':'规矩'}]+[{'role':'user','content':'禁止发布'+'x'*4000} for _ in range(8)]
    result=await compaction.maybe_compact(messages,models=M(),role='main',context_window=8192,output_reserve=256,purpose='test')
    assert result==messages

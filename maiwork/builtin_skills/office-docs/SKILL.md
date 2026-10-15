---
name: office-docs
description: 要交 Word / Excel / PPT / PDF 时用它：先查执行环境里有哪些库和 LibreOffice，再用 python-docx / openpyxl / python-pptx / soffice 按标准做法产出，交付前逐条过自检清单（文件真能打开、没有占位符、数字对得上来源、公式算过没有）。任务要产办公文档（报告、表格、演示、PDF）时使用。
license: AGPL-3.0-or-later
metadata:
  maiwork-roles: worker
  maiwork-builtin: "true"
  version: "1.0"
  maiwork-source: "自研（python-docx / openpyxl / python-pptx / LibreOffice）"
---

# 办公文档（Word / Excel / PPT / PDF）

自研标准做法，不依赖任何第三方文档 skill。核心只有一句：**先查环境，再动手；产出后必须自己
重新打开读一遍**。

## 什么时候用

- 计划要交 .docx / .xlsx / .pptx / .pdf；
- 群友给了一个表格 / 文档，要改、要汇总、要重排；
- 要把一份内容同时做成文档和 PDF。

## 第一步：先查环境（不许假设库在）

用 `run_command` 查一遍，把结果贴进交回总结（**装过一键装之后也是同一条命令**：装完
下一条本机命令就 import 得到了）：

```bash
python -c "import docx; print('python-docx', docx.__version__ if hasattr(docx,'__version__') else 'ok')"
python -c "import openpyxl; print('openpyxl', openpyxl.__version__)"
python -c "import pptx; print('python-pptx', pptx.__version__)"
python -c "import fitz; print('pymupdf ok')" 2>/dev/null || echo "no pymupdf"
which soffice || which libreoffice || echo "no libreoffice"
```

- **有库**：直接写脚本产出，产物写到 `artifacts/<任务ID>/`。
- **缺库**：按代价从低到高选：
  1. **让管理员一键装**（最省事，先提这一条）：办公文档三库（python-docx / openpyxl /
     python-pptx）是**可选组件**，管理员在网页「设置 → 工具」点一下就能装。装的是 MaiWork
     自己的目录（不是 MaiBot 的环境），装完**下一条本机命令**就能 `import` 到，不用改脚本、
     不用换环境。交回里写一句「这台机器还没装这三个库，请在网页『设置 → 工具』一键装，
     装完我就能直接产出」就够了。
  2. **换交付形态**（不用等任何人）：Markdown / CSV / HTML，并在交回里说明为什么换。
  3. **去一次性机器做**：用 `vm_run` 装依赖做完，再用 `vm_fetch_file` 把成品拷回本机工作区
     —— **成品只留在机器上是交不出去的**。专用机器同理走 `machine_run`。
- **不要自己 `pip install`**：本机命令跑在只读的隔离环境里，装不了东西，白烧一轮。
- **LibreOffice（soffice）不在一键装的范围里**：它是系统级软件，MaiWork 不替用户装。
  没有它就别承诺 PDF / 公式重算，按下面各节写明的回落办法处理，并在交回里说明。
- 不许「先写着，反正能跑」：库不在的脚本一定会报错，白烧一轮。

## Word（python-docx）

```python
from docx import Document
from docx.shared import Pt
from docx.oxml.ns import qn

doc = Document()
doc.add_heading("标题", level=1)          # 用 Heading 样式，不要手工调大字号当标题
p = doc.add_paragraph("正文")
doc.add_table(rows=1, cols=3).style = "Table Grid"
doc.save("artifacts/<任务ID>/报告.docx")
```

- 结构用**内置样式**（Heading 1/2/3、Title、List Bullet），别用空格和加粗硬凑层级；
- 表格用 add_table + Table Grid 样式；表头行自己加粗；
- 中文要设东亚字体，否则可能变成默认宋体以外的字体：

```python
style = doc.styles["Normal"]
style.font.name = "宋体"
style.font.size = Pt(11)
style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
```

- 需要 PDF：用 LibreOffice 转（见下），不要另写一套。

## Excel（openpyxl）

```python
from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws.title = "汇总"
ws.append(["项目", "数量", "占比"])
ws.append(["甲", 12, "=B2/$B$2"])          # 公式就写公式，不要写死结果
ws.freeze_panes = "A2"
ws.column_dimensions["A"].width = 18
wb.save("artifacts/<任务ID>/汇总.xlsx")
```

- **公式写公式、不要写死结果**：写死的数字以后一改就错。占比、合计、平均值都用公式。
- 公式必须**重算过再交**：用 LibreOffice 让公式算出来，再用 openpyxl 以只读数据模式回读关键
  单元格核对（脚本里用 `load_workbook(path, data_only=True)` 读值）：

```bash
soffice --headless --convert-to xlsx --outdir artifacts/<任务ID>/recalc artifacts/<任务ID>/汇总.xlsx
```

  没有 LibreOffice 时：公式照写，但要在交回里写明「公式未在本机重算，用 Excel 打开时自动计算」，
  并且不要把没算过的公式当成「结果」写进交付说明。
- 表头冻结（freeze_panes）、列宽合适、数字用数字格式（金额 / 百分比不要写成一串文本）；
- 一个表一件事，不要把所有数据塞进一个 sheet；sheet 名用中文、简短。

## PPT（python-pptx）

```python
from pptx import Presentation
from pptx.util import Inches

prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])   # 标题+内容版式
slide.shapes.title.text = "一页一个结论"
slide.placeholders[1].text = "支撑这页的三条要点"
prs.save("artifacts/<任务ID>/演示.pptx")
```

- **一页一个结论**，标题就是结论句（不要写「背景介绍」这种栏目名）；
- 用版式（slide_layouts）而不是手工摆文本框；
- 正文不超过 6 行、每行不超过 20 字；字号不小于 18；
- 图片按比例缩放进占位区，不要拉伸变形；图注写清来源。

## PDF

- **首选**：先做出 .docx / .pptx，再用 LibreOffice 转：

```bash
soffice --headless --convert-to pdf --outdir artifacts/<任务ID>/ artifacts/<任务ID>/报告.docx
```

- 只要 PDF（不交源文件）时也保留中间产物，方便验收与改版；
- 中文 PDF 要确认字体已嵌入：转换后打开看一眼有没有方框 / 乱码；
- 不要用画图库（reportlab 之类）硬画长文：分页、断行、目录都要自己扛，容易出错。

## 交付前自检（逐条过）

1. **文件真在、不为空**：用 `list_files` / `read_file` 确认路径与大小。
2. **能打开**：用对应的库重新打开读一遍（`Document(...)` / `load_workbook(...)` /
   `Presentation(...)` / `fitz.open(...)`），把读到的页数 / 表数 / 幻灯片数写进交回。
3. **没有占位符**：搜一遍 TODO、待补充、示例数据、XXX、「此处不写」。
4. **数字对得上**：每个数字能指到来源或计算过程；Excel 里的合计与明细能对上。
5. **公式算过**：重算后回读核对，或写明未重算。
6. **中文正常**：字体设置过了、PDF 里没有方框乱码。
7. **文件名与内部用语**：文件名是给群友看的（不含任务号、不含 `artifacts/` 这类路径）；
   正文里不出现「主模型」「子 agent」「job 编号」。
8. **形态对**：要 PDF 就交 PDF，要源文件就一起交；不要只交中间脚本。

## 怎么交回

- 用 `submit_result` 交回：成品路径、页数 / 表数 / 幻灯片数、环境自检结果（有哪些库、
  有没有 LibreOffice）、没做到的地方与原因。
- 产物一律在 `artifacts/<任务ID>/` 下；写脚本也用 `write_file` 写到工作区里，别在临时目录里写。
- **不能宣布任务完成**：完成与否由主流程验收。

## 来源与说明

- 自研（AGPL-3.0-or-later，随 MaiWork 发布）。
- 明确不采用 anthropics/skills 的 docx / xlsx / pptx / pdf 四份：它们是 source-available 的
  专有许可，禁复制、禁衍生、禁分发。这份是从零按 python-docx / openpyxl / python-pptx /
  LibreOffice 的官方用法写的。

# 案例裁判说理核查

案例身份已经由系统确定，不得重新判断案号、案名、法院或案例身份，不得使用模型记忆补充案情或裁判观点。

输入包含：

- `assertions`：文书归于该案例的观点，已切分为带编号的观点句；
- `reasoning_sentences`：该案裁判说理（判决书"本院认为"部分的节选）的带编号句子列表，这是唯一权威依据；
- `reasoning_truncated`：说理文本是否疑似在结尾被截断。

权威说理文本经过匿名化处理，"某某""××"以及法律名称中的脱敏符号（如"中华某某共和国"）不构成与文书表述的不一致；文书使用真实姓名或公司名而说理使用脱敏名，属于同一对象。

## 任务

对每个观点句独立判断，只允许四种结论：

- `supported`：说理中有句子讨论并支持该观点，转述忠实。必须在 `hit_sentence_ids` 给出支持句的编号。
- `distorted`：说理中有句子讨论该观点，但文书转述改变、反转或不当扩张了其含义。必须在 `hit_sentence_ids` 给出被曲解句子的编号，并说明差异。
- `unsupported`：逐句检查后，说理中没有任何句子讨论该观点。`hit_sentence_ids` 必须为空数组。
- `not_holding`：该句只陈述案例名称、发布机关、发布日期等案例编目信息，不属于裁判说理观点。`hit_sentence_ids` 必须为空数组，系统将交由案例身份核查处理。

规则：

1. `hit_sentence_ids` 只能填 `reasoning_sentences` 中真实存在的编号，按相关程度排列，通常 1–3 个。不得虚构编号。
2. 逐字不同不等于曲解。概括、换序、省略与观点无关的内容，以及不改变结论的事实细化均属正常转述。仅当遗漏或增删改变了限定条件、前提、结论方向或适用范围时才是 `distorted`；`diff_summary` 只写这些实质差异，不夹带不影响结论的措辞差异。
3. 判断 `unsupported` 前必须逐句排查全部说理句。若 `reasoning_truncated` 为 true，说理可能不完整，仍按现有句子判断，截断风险由系统处理。
4. 不评价文书法律论证是否成立，不评价该案例是否具有约束力。
5. 权威说理以多个条件共同得出结论时，转述若省略其中的必要前提，并把剩余条件单独写成充分条件，必须判为 `distorted`；不得因权威文本也提到剩余条件而判为支持。
6. 阿拉伯数字与等值中文数字表达含义相同；文书只摘录其中一项判决结果时，不得因未同时列出其他互不冲突的判项而判为曲解。

## 语言规范

`diff_summary` 具体说明：文书归纳了什么观点、说理原文（引用编号句）实际表达了什么、差异为何改变含义。`suggestion` 是展示给用户的自包含文案，写清"具体问题＋建议动作"，原则上不超过70字，不重复整段说理原文。只有能严格依据说理原文形成唯一、完整的最小修订时才给出 `revised_text`（修订后的完整观点表述），否则为 `null`。

## 输出

仅输出一个可由 `JSON.parse()` 解析的 JSON 对象，不输出 Markdown 或解释：

{"verdict":"pass|issue|insufficient_input","assertions":[{"id":1,"judgment":"supported|distorted|unsupported|not_holding","hit_sentence_ids":[2,3],"risk_level":"HIGH|MEDIUM","diff_summary":"...","suggestion":"...","revised_text":null}],"notes":""}

- 每个输入观点句必须恰好对应一个输出项，`id` 与输入一致。
- 全部 `supported` 时 `verdict` 为 `pass`；存在 `distorted` 或 `unsupported` 时为 `issue`。
- `risk_level`、`diff_summary`、`suggestion` 仅 `distorted` 和 `unsupported` 需要提供。
- `insufficient_input`：仅在观点句无法独立理解、说理文本明显不可读或输入结构冲突时使用，`notes` 写明具体原因。

所有文本字段只允许面向用户的自然语言。不得输出内部锚点、line 编号、`⟦...⟧`、`[[...]]` 或锚点标签。

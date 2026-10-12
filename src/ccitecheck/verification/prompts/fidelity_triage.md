# 引文忠实性与位置分诊

输入包含一段 `quote`、可能存在的 `cited`，以及至多五组 `candidates`。每个来源都有唯一 `id` 和权威条文正文；同组候选可能对应多个位置。候选顺序没有意义。

只依据输入正文判断哪一个来源完整支持引文，不凭记忆补充法律、版本或条号。逐项核对：

1. 主体；
2. 必要条件、例外和适用范围；
3. 数字、期限及数量关系；
4. 否定词和规范强度；
5. 权利、义务及法律后果。

忠实转述、代词的正确展开、语序调整和不改变含义的措辞变化可以支持。遗漏必要条件、改变主体、数字、否定、规范强度或法律后果均不支持。

- 所引正文完整支持时，`verdict` 为 `cited`。
- 某一候选组完整支持时，`verdict` 为该组输入中的 `id`。
- 没有任何来源完整支持时，`verdict` 为 `none`，并在 `differences` 中列出具体实质差异。
- 只能返回输入中存在的 ID。不得自行编写法名、版本或条号作为 verdict。
- `sources` 中的 `temporal_status` 是权威时效标签；多个来源同时完整支持时，优先选择含 `current` 来源的候选，不得优先选择 `obsolete` 来源。
- 选择来源时五项 checks 必须全部为 true，differences 必须为空；verdict 为 none 时必须给出具体差异。

仅输出一个可由 `JSON.parse()` 解析的 JSON 对象：

{"verdict":"cited|candidate_1|none","checks":{"subject":true,"condition":true,"numbers":true,"negation":true,"consequence":true},"differences":[]}

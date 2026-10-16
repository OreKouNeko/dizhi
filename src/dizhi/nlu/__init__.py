"""自然语言理解层：把一句中文变成结构化约束。

两个可互换的实现：
    rule.parse(text)                     —— 规则式，零依赖、离线、快
    llm.parse_with_llm(text)             —— 交给大模型，容错强，需要 API key

两者返回类型一致（`list[Constraint]`），上层代码不用改。
"""

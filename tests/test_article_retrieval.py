from ccitecheck.retrieval.ranking import retrieve_relevant_articles


def test_retrieval_ranks_substantively_matching_article_first():
    articles = [
        {
            "article_no": "第一条",
            "article_key": "1",
            "text": "为了保障网络安全，维护国家安全和社会公共利益，保护公民、法人和其他组织的合法权益。",
        },
        {
            "article_no": "第二条",
            "article_key": "2",
            "text": "在中华人民共和国境内建设、运营、维护和使用网络。",
        },
    ]

    results = retrieve_relevant_articles(
        "保障网络安全和公民、法人合法权益，维护国家安全和公共利益。",
        articles,
    )

    assert results[0].article_no == "第一条"
    assert results[0].relevance_score > results[1].relevance_score

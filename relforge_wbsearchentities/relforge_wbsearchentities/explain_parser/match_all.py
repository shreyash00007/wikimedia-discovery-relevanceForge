from relforge_wbsearchentities.explain_parser.core import (
    register_parser,

    BaseExplainParser,
    IncorrectExplainException,
    PassThruExplain,
)
from relforge_wbsearchentities.explain_parser.utils import MATCH_ALL_EXPLAIN


class MatchAllExplainParser(BaseExplainParser):
    @staticmethod
    @register_parser('match_all')
    def from_query(options, name_prefix):
        assert options == {}
        return MatchAllExplainParser(name_prefix)

    def constant_score_desc(self):
        return '*:*'

    def parse(self, lucene_explain):
        if lucene_explain != MATCH_ALL_EXPLAIN:
            raise IncorrectExplainException('Not a match_all explain')
        explain = PassThruExplain(lucene_explain, name='match_all', name_prefix=self.name_prefix)
        explain.parser_hash = hash(self)
        return explain

    def merge(self, a, b):
        return a

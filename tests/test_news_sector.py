"""Tests for app.news.sector: SectorMapper."""
from app.news.sector import SectorMapper


class TestSectorMapper:
    def setup_method(self):
        self.mapper = SectorMapper()

    def test_loads_default_dict(self):
        assert len(self.mapper._sector_to_stocks) > 0
        assert len(self.mapper._stock_to_sectors) > 0

    def test_exact_match(self):
        codes = self.mapper.match_sector("半导体")
        assert len(codes) > 0
        assert "sh688981" in codes

    def test_substring_match_input_in_key(self):
        codes = self.mapper.match_sector("新能源车")
        assert len(codes) > 0
        assert "sz002594" in codes

    def test_substring_match_key_in_input(self):
        codes = self.mapper.match_sector("银行板块")
        assert len(codes) > 0
        assert "sh601398" in codes

    def test_fuzzy_match_with_typo(self):
        codes = self.mapper.match_sector("锂电")
        assert len(codes) > 0

    def test_no_match_returns_empty(self):
        assert self.mapper.match_sector("完全不存在的板块XYZ123") == []

    def test_empty_input_returns_empty(self):
        assert self.mapper.match_sector("") == []

    def test_get_stock_sectors(self):
        sectors = self.mapper.get_stock_sectors("sh600519")
        assert "白酒" in sectors
        assert "食品饮料" in sectors

    def test_get_stock_sectors_unknown_stock(self):
        assert self.mapper.get_stock_sectors("sh000000") == []

    def test_map_analysis_combines_sectors_and_stocks(self):
        codes = self.mapper.map_analysis(["半导体", "银行"], ["sh600519"])
        assert "sh688981" in codes      # from 半导体
        assert "sh601398" in codes      # from 银行
        assert "sh600519" in codes      # from LLM stocks
        assert len(codes) == len(set(codes))

    def test_map_analysis_with_unknown_sector(self):
        codes = self.mapper.map_analysis(["不存在板块"], ["sh600519"])
        assert codes == ["sh600519"]

    def test_map_analysis_dedup(self):
        codes = self.mapper.map_analysis(["半导体", "集成电路"], [])
        assert len(codes) == len(set(codes))

    def test_map_analysis_skips_lowercase_stock(self):
        codes = self.mapper.map_analysis([], ["sh600519", "abc"])
        assert "sh600519" in codes
        assert "abc" not in codes


class TestSectorMapperMissingFile:
    def test_missing_cache_file_loads_empty(self, tmp_path):
        from app.news.sector import SectorMapper
        m = SectorMapper(cache_path=tmp_path / "missing.json")
        assert m._sector_to_stocks == {}
        assert m._stock_to_sectors == {}

    def test_malformed_cache_file_loads_empty(self, tmp_path):
        from app.news.sector import SectorMapper
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{")
        m = SectorMapper(cache_path=bad)
        assert m._sector_to_stocks == {}
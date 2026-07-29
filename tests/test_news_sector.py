"""Tests for app.news.sector: SectorMapper (returns Stock)."""
from app.news.models import Stock
from app.news.sector import SectorMapper


class TestSectorMapper:
    def setup_method(self):
        self.mapper = SectorMapper()

    def test_loads_default_dict(self):
        assert len(self.mapper._sector_to_stocks) > 0
        assert len(self.mapper._stock_to_sectors) > 0

    def test_exact_match(self):
        stocks = self.mapper.match_sector("半导体")
        codes = {s.code for s in stocks}
        assert "sh688981" in codes

    def test_substring_match_input_in_key(self):
        stocks = self.mapper.match_sector("新能源车")
        codes = {s.code for s in stocks}
        assert "sz002594" in codes

    def test_substring_match_key_in_input(self):
        stocks = self.mapper.match_sector("银行板块")
        codes = {s.code for s in stocks}
        assert "sh601398" in codes

    def test_fuzzy_match_with_typo(self):
        stocks = self.mapper.match_sector("锂电")
        assert len(stocks) > 0

    def test_no_match_returns_empty(self):
        assert self.mapper.match_sector("完全不存在的板块XYZ123") == []

    def test_empty_input_returns_empty(self):
        assert self.mapper.match_sector("") == []

    def test_match_returns_stock_objects_with_names(self):
        stocks = self.mapper.match_sector("银行")
        assert all(isinstance(s, Stock) for s in stocks)
        named = [s for s in stocks if s.name]
        assert len(named) >= 5

    def test_bank_stocks_have_chinese_names(self):
        stocks = self.mapper.match_sector("银行")
        names = [s.name for s in stocks if s.name]
        assert "工商银行" in names
        assert "招商银行" in names

    def test_get_stock_sectors(self):
        sectors = self.mapper.get_stock_sectors("sh600519")
        assert "白酒" in sectors
        assert "食品饮料" in sectors

    def test_get_stock_sectors_unknown_stock(self):
        assert self.mapper.get_stock_sectors("sh000000") == []

    def test_map_analysis_combines_sectors_and_stocks(self):
        stocks = self.mapper.map_analysis(
            ["半导体", "银行"],
            [Stock(code="sh600519", name="贵州茅台")],
        )
        codes = {s.code for s in stocks}
        assert "sh688981" in codes
        assert "sh601398" in codes
        assert "sh600519" in codes

    def test_map_analysis_dedup(self):
        stocks = self.mapper.map_analysis(["半导体", "集成电路"], [])
        codes = [s.code for s in stocks]
        assert len(codes) == len(set(codes))

    def test_map_analysis_skips_legacy_strings(self):
        stocks = self.mapper.map_analysis([], ["sh600519"])
        assert any(s.code == "sh600519" for s in stocks)


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
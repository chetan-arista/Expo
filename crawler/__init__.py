"""
Universal Website Crawler Tool
Recursively crawls any website, discovers all internal links, handles sitemaps and dynamic JS,
and exports results to a professional formatted Excel (.xlsx) file.
"""

from .config import CrawlConfig
from .engine import WebsiteCrawler, CrawlResult, ExternalLink
from .exporter import ExcelExporter

__all__ = ["CrawlConfig", "WebsiteCrawler", "CrawlResult", "ExternalLink", "ExcelExporter"]

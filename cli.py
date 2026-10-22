import argparse
import sys
import os
import time

from crawler.config import CrawlConfig
from crawler.engine import WebsiteCrawler, ExternalLink
from crawler.exporter import ExcelExporter, EXPORT_CATEGORIES
from crawler.issues import ISSUE_CATEGORIES, issue_sort_key
from crawler.url_utils import get_domain

MAX_SCREENSHOT_ISSUES = 25

def write_pdf_report(crawler: WebsiteCrawler, excel_path: str, screenshots: bool = True) -> str:
    """Builds <excel name>_issues.pdf, with screenshots of the most severe issues when Chrome is available."""
    from crawler.evidence import EvidenceCollector
    from crawler.pdf_report import build_pdf_report

    base = os.path.splitext(excel_path)[0]
    shots_dir = base + "_evidence"
    evidence, note = {}, ""
    issues = sorted([r for r in crawler.results_list if r.category in ISSUE_CATEGORIES], key=issue_sort_key)
    if issues and screenshots:
        collector = EvidenceCollector(
            out_dir=shots_dir,
            user_agent=crawler.config.user_agent,
            timeout=crawler.config.timeout,
            cookies=crawler.auth_cookies,
            logger=print
        )
        if collector.start():
            print(f"Capturing screenshots for {min(len(issues), MAX_SCREENSHOT_ISSUES)} issue(s)...")
            try:
                evidence = collector.capture(issues[:MAX_SCREENSHOT_ISSUES])
            finally:
                collector.close()
        else:
            note = f"Screenshots unavailable: {collector.unavailable_reason}"
            print(note)
    elif not screenshots:
        note = "Screenshots were disabled for this crawl."

    pdf_path = base + "_issues.pdf"
    build_pdf_report(crawler.results_list, pdf_path, crawler.get_stats(), evidence, shots_dir, note)
    return pdf_path

def main():
    parser = argparse.ArgumentParser(
        description="Universal Website Internal & Outbound Link Crawler & Excel Exporter"
    )
    parser.add_argument("url", nargs="?", default="", help="Website URL to crawl (e.g. https://aristasystems.in/)")
    parser.add_argument("-o", "--output", help="Path to output Excel (.xlsx) file")
    parser.add_argument("-m", "--max-urls", type=int, default=1000, help="Maximum internal URLs to crawl (0 = unlimited, default: 1000)")
    parser.add_argument("-d", "--max-depth", type=int, default=10, help="Maximum link depth (default: 10)")
    parser.add_argument("-t", "--threads", type=int, default=8, help="Concurrent worker threads (default: 8)")
    parser.add_argument("--no-sitemap", action="store_true", help="Disable sitemap discovery")
    parser.add_argument("--no-external", action="store_true", help="Disable collecting external outbound links")
    parser.add_argument("--dynamic", action="store_true", help="Enable Selenium Chrome for JavaScript dynamic links")
    parser.add_argument("--delay", type=float, default=0.0, help="Polite delay between requests in seconds")
    parser.add_argument("--subdomains", action="store_true", help="Include subdomains as internal links")
    parser.add_argument("--verify-ssl", action="store_true", help="Enforce strict SSL certificate verification")
    parser.add_argument("--category", default="all", choices=sorted(EXPORT_CATEGORIES),
                        help="Excel scope: all (default), 2xx, 3xx, 4xx, 5xx, error, issues or external")
    parser.add_argument("--pdf", action="store_true", help="Also create a visual PDF issue report")
    parser.add_argument("--no-screenshots", action="store_true", help="PDF without screenshots (no Chrome needed)")

    args = parser.parse_args()

    if not args.url:
        print("\n=== Universal Website Link Crawler ===")
        url_input = input("Enter Website URL (e.g. https://aristasystems.in/): ").strip()
        if not url_input:
            print("Error: No URL provided.")
            sys.exit(1)
        args.url = url_input

    if not args.url.startswith("http://") and not args.url.startswith("https://"):
        args.url = "https://" + args.url

    domain = get_domain(args.url) or "website"
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = args.output or f"crawl_{domain}_{timestamp}.xlsx"

    config = CrawlConfig(
        start_url=args.url,
        max_urls=args.max_urls,
        max_depth=args.max_depth,
        concurrency=args.threads,
        use_sitemaps=not args.no_sitemap,
        collect_external_links=not args.no_external,
        use_dynamic_js=args.dynamic,
        delay=args.delay,
        include_subdomains=args.subdomains,
        verify_ssl=args.verify_ssl,
        output_file=output_path
    )

    print(f"\n========================================================")
    print(f" Target Website    : {config.start_url}")
    print(f" Output File       : {os.path.abspath(output_path)}")
    print(f" Concurrency       : {config.concurrency} threads")
    print(f" Sitemaps Probe    : {'Enabled' if config.use_sitemaps else 'Disabled'}")
    print(f" External Links    : {'Collect without crawling' if config.collect_external_links else 'Disabled'}")
    print(f" Dynamic JS DOM    : {'Enabled (Selenium)' if config.use_dynamic_js else 'Disabled'}")
    print(f" Max URLs Limit    : {'Unlimited' if config.max_urls == 0 else config.max_urls}")
    print(f"========================================================\n")

    def on_result(result):
        code_str = str(result.status_code) if result.status_code else "ERR"
        title_disp = (result.page_title[:42] + '..') if len(result.page_title) > 44 else result.page_title
        print(f"[{code_str:>3}] (D:{result.depth}) [Internal] {result.url} | {title_disp}", flush=True)

    def on_external_link(ext: ExternalLink):
        # Concise indicator when an external URL is found and recorded
        pass

    def on_log(msg):
        print(msg, flush=True)

    crawler = WebsiteCrawler(
        config=config,
        on_result=on_result,
        on_external_link=on_external_link,
        on_log=on_log
    )

    try:
        results = crawler.run()
        stats = crawler.get_stats()
        
        print("\n" + "="*58)
        print(" Crawl Summary:")
        print(f" Total Internal Discovered : {stats['total_discovered']}")
        print(f" Total Internal Crawled    : {stats['total_crawled']}")
        print(f" 2xx Success (Internal)    : {stats['status_2xx']}")
        print(f" 3xx Redirects             : {stats['status_3xx']}")
        print(f" 4xx Broken Links          : {stats['status_4xx']}")
        print(f" 5xx / Errors              : {stats['status_5xx'] + stats['status_err']}")
        print(f" External Links Collected  : {stats.get('total_external', 0)} ({stats.get('unique_external_domains', 0)} unique domains)")
        print(f" (Note: External sites are recorded but NOT crawled)")
        print(f" Elapsed Time              : {stats['elapsed_seconds']}s")
        print("="*58)

        # Export to Excel
        print(f"\nExporting to Excel spreadsheet: {output_path} ...")
        ExcelExporter.export(
            results=results,
            filepath=output_path,
            stats=stats,
            external_links=crawler.external_links_list,
            category=args.category
        )
        print(f"[SUCCESS] Excel file saved successfully at: {os.path.abspath(output_path)}")
        if args.pdf:
            pdf_path = write_pdf_report(crawler, output_path, screenshots=not args.no_screenshots)
            print(f"[SUCCESS] PDF issue report saved at: {os.path.abspath(pdf_path)}")

    except KeyboardInterrupt:
        print("\n[Interrupt] Stopping crawler gracefully...")
        crawler.stop()
        results = crawler.results_list
        if results or crawler.external_links_list:
            ExcelExporter.export(
                results=results,
                filepath=output_path,
                stats=crawler.get_stats(),
                external_links=crawler.external_links_list
            )
            print(f"[Saved partial results to {output_path}]")
        sys.exit(0)

if __name__ == "__main__":
    main()

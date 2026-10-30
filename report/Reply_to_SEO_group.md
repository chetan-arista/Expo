Hi all,

We compared the new ARACHNIX report (69 issues, crawl of 28 Oct 18:02) with Screaming Frog's (39 issues), issue by issue.

- **Both tools are counting correctly.** Most differences come from the checks each tool runs and the pages each one reached.
- **38 of Screaming Frog's 39 issues are also in ARACHNIX.** 14 have exactly the same count (for example 80/80 noindex pages, 99/99 multiple meta descriptions, 12/12 non-descriptive link text, 80/80 pages with many external links). Most of the other 24 are close and explained. ARACHNIX reached a few more pages, including test pages, and it also checks CSS background images and responsive image sizes.
- **1 issue is only in Screaming Frog: "Soft 404" (1 page).** The only page that could trigger it is our real service page "404 Cleanup & Categorization Structure". We think Screaming Frog flagged it by mistake. Could you send us the URL Screaming Frog lists so we can confirm?
- **31 issues are only in ARACHNIX:** 18 are checks Screaming Frog doesn't run (speed, page hygiene, social sharing tags, accessibility, links only in menus). 5 are Screaming Frog checks that are off by default (sitemaps, structured data, broken #bookmarks). 7 are pages or links Screaming Frog didn't report (test pages, external links that forward visitors elsewhere, the javascript: links on /careers/). 1 is our mistake (next point).
- **Our mistake:** the "External Server Error (5xx)" is our LinkedIn page. LinkedIn sends code 999 to block automated checkers, and the link works fine for people. This is fixed: the link will now show as "Blocked". We also stopped counting the javascript: links on /careers/ twice. After the next server restart the report will show 67 issues.
- **Crawl depth:** "Pages With High Crawl Depth" is 9 for us and 40 in Screaming Frog. Both tools use the same rule (more than 4 clicks from the home page) but measure depth differently. Could you share Screaming Frog's list of those 40 pages?
- The older 58-issue report was missing 7 checks because our server hadn't loaded them. That is fixed, so the new report includes them.

Details for every issue are in **ARACHNIX_vs_ScreamingFrog_issue_reconciliation.xlsx**.

Thanks,

import asyncio
from playwright.async_api import async_playwright
import aiohttp
import os
from urllib.parse import urljoin, urlparse
import re
import csv


async def download_file(session, url, filepath, headers):
    """Download a single file asynchronously."""
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as response:
            if response.status == 200:
                with open(filepath, 'wb') as f:
                    f.write(await response.read())
                return True
            else:
                print(f"  Failed to download {url}: Status {response.status}")
                return False
    except Exception as e:
        print(f"  Error downloading {url}: {str(e)}")
        return False


async def extract_subpage_links(page):
    """Extract all IOM subpage links from the current listing page."""
    links = await page.evaluate('''() => {
        const results = [];
        // Grab all anchors on the page that point to IOM item pages
        const anchors = document.querySelectorAll('a[href]');
        anchors.forEach(a => {
            const href = a.getAttribute('href');
            const text = a.innerText.trim();
            if (href && href.toLowerCase().includes('internet-only-manuals-ioms-items/')) {
                results.push({ url: href, title: text });
            }
        });
        return results;
    }''')
    return links


async def extract_pdfs_from_subpage(page, subpage_url, base_url):
    """Navigate to a subpage and extract all PDF links."""
    try:
        await page.goto(subpage_url, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(1500)

        pdf_links = await page.evaluate('''() => {
            const links = [];
            const anchors = document.querySelectorAll('a[href]');
            anchors.forEach(a => {
                const href = a.getAttribute('href') || '';
                const text = (a.innerText || '').trim();
                if (href.toLowerCase().includes('.pdf')) {
                    links.push({
                        url: href,
                        linkText: text,
                        filename: href.split('/').pop().split('?')[0]
                    });
                }
            });
            return links;
        }''')

        # Normalize URLs
        for link in pdf_links:
            link['url'] = urljoin(base_url, link['url'])
        return pdf_links
    except Exception as e:
        print(f"  Error loading subpage {subpage_url}: {e}")
        return []


async def scrape_cms_manuals():
    base_url = "https://www.cms.gov"
    start_url = "https://www.cms.gov/medicare/regulations-guidance/manuals/internet-only-manuals-ioms"

    download_dir = "cms-medicare_manuals"
    os.makedirs(download_dir, exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    total_pages = 3  # Adjust as needed
    subpage_links = {}   # url -> {title, ...}
    all_pdf_links = {}   # url -> metadata

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=headers['User-Agent'])

        # ---- STAGE 1: Collect subpage links from the listing pages ----
        print(f"STAGE 1: Collecting subpage links from {total_pages} listing pages\n")
        for page_num in range(total_pages):
            page = await context.new_page()
            page_url = start_url if page_num == 0 else f"{start_url}?page={page_num}"

            print(f"[Listing {page_num + 1}/{total_pages}] {page_url}")
            try:
                await page.goto(page_url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(2000)

                # Try to wait for the table, but don't fail if the selector differs
                try:
                    await page.wait_for_selector('table', timeout=10000)
                except Exception:
                    pass

                found = await extract_subpage_links(page)
                new_count = 0
                for link in found:
                    full_url = urljoin(base_url, link['url'])
                    if full_url not in subpage_links:
                        subpage_links[full_url] = {
                            'title': link['title'] or 'Unknown',
                            'listing_page': page_num + 1
                        }
                        new_count += 1
                print(f"  Found {len(found)} subpage links ({new_count} new). "
                      f"Total unique: {len(subpage_links)}")
            except Exception as e:
                print(f"  Error on listing page {page_num + 1}: {e}")
            finally:
                await page.close()
                await asyncio.sleep(1)

        print(f"\nCollected {len(subpage_links)} unique subpages\n")

        # ---- STAGE 2: Visit each subpage and extract PDFs ----
        print(f"STAGE 2: Extracting PDF links from each subpage\n")
        subpage_page = await context.new_page()
        for idx, (sub_url, meta) in enumerate(subpage_links.items(), 1):
            print(f"[Subpage {idx}/{len(subpage_links)}] {sub_url}")
            pdfs = await extract_pdfs_from_subpage(subpage_page, sub_url, base_url)
            print(f"  Found {len(pdfs)} PDF(s)")

            for pdf in pdfs:
                if pdf['url'] not in all_pdf_links:
                    all_pdf_links[pdf['url']] = {
                        'url': pdf['url'],
                        'filename': pdf['filename'],
                        'linkText': pdf['linkText'],
                        'title': meta['title'],
                        'subpage_url': sub_url,
                        'listing_page': meta['listing_page'],
                    }
            await asyncio.sleep(0.5)

        await subpage_page.close()
        await browser.close()

    all_pdf_list = list(all_pdf_links.values())
    print(f"\n{'='*60}")
    print(f"Total unique PDF files found: {len(all_pdf_list)}")
    print(f"{'='*60}\n")

    if not all_pdf_list:
        print("WARNING: No PDFs found. Inspect the DOM for changes.")
        return

    # Save metadata CSV
    metadata_file = os.path.join(download_dir, 'download_metadata.csv')
    with open(metadata_file, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['filename', 'title', 'url', 'subpage_url', 'listing_page', 'linkText']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for link in all_pdf_list:
            writer.writerow(link)
    print(f"Metadata saved to: {metadata_file}\n")

    # ---- STAGE 3: Download PDFs ----
    async with aiohttp.ClientSession() as session:
        for idx, file_info in enumerate(all_pdf_list, 1):
            raw_name = file_info['filename'] or urlparse(file_info['url']).path.split('/')[-1]
            filename = re.sub(r'[<>:"/\\|?*]', '_', raw_name)
            if not filename.lower().endswith('.pdf'):
                filename += '.pdf'
            filepath = os.path.join(download_dir, filename)

            if os.path.exists(filepath):
                print(f"[{idx}/{len(all_pdf_list)}] ✓ Already exists: {filename}")
                continue

            print(f"[{idx}/{len(all_pdf_list)}] Downloading: {filename}")
            success = await download_file(session, file_info['url'], filepath, headers)
            print(f"  {'✓ Saved' if success else '✗ Failed'}: {filename}")
            await asyncio.sleep(0.5)

    print(f"\n✓ Complete! Files saved to '{download_dir}'")


if __name__ == "__main__":
    asyncio.run(scrape_cms_manuals())


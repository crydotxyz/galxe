import aiohttp
import asyncio
import random
import os
import time
import uuid
from datetime import datetime

from termcolor import cprint
from loguru import logger
from typing import Tuple
from eth_account import Account as EthAccount

from internal.storage import AccountStorage
from internal.models import AccountInfo
from internal.twitter import Twitter
from internal.config import THREADS_NUM, CHECKER_UPDATE_STORAGE
from internal.utils import async_retry, log_long_exc


# Generate a unique session ID for this run
SESSION_ID = str(uuid.uuid4())[:8]
START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@async_retry
async def change_ip(link: str):
    async with aiohttp.ClientSession() as sess:
        async with sess.get(link) as resp:
            if resp.status != 200:
                raise Exception(f'Failed to change ip: Status = {resp.status}. Response = {await resp.text()}')


def load_replies():
    try:
        with open('files/reply.txt', 'r', encoding='utf-8') as file:
            replies = file.read().splitlines()
            return [r.strip() for r in replies if r.strip()]
    except FileNotFoundError:
        logger.error("files/reply.txt not found. Please create it with your reply messages.")
        return ["LFGGG"]  # Default fallback message


def save_reply_id(username: str, tweet_url: str, reply_url: str):
    """Save reply ID with username prefix to replyid.txt with session info"""
    try:
        # Create results directory if it doesn't exist
        os.makedirs('results', exist_ok=True)
        
        # Write session header if this is first write for this session
        if not hasattr(save_reply_id, 'session_header_written'):
            with open('results/replyid.txt', 'a', encoding='utf-8') as file:
                file.write(f"\n{'='*50}\n")
                file.write(f"Session ID: {SESSION_ID}\n")
                file.write(f"Start Time: {START_TIME}\n")
                file.write(f"Target Tweet URL: {tweet_url}\n")
                file.write(f"{'='*50}\n\n")
            save_reply_id.session_header_written = True

        # Save the reply info
        with open('results/replyid.txt', 'a', encoding='utf-8') as file:
            current_time = datetime.now().strftime("%H:%M:%S")
            file.write(f"Time: {current_time}\n")
            file.write(f"Target (@{username}): {tweet_url}\n")
            file.write(f"Reply: {reply_url}\n")
            file.write("-" * 50 + "\n")
    except Exception as e:
        logger.error(f"Failed to save reply ID: {str(e)}")


async def safe_reply(twitter: Twitter, tweet_url: str, message: str, idx: int, reply_idx: int, total_replies: int) -> bool:
    """Safely attempt to reply to a tweet, handling various error cases."""
    try:
        reply_url = await twitter.reply_to_tweet(tweet_url, message)
        logger.info(f'{idx}) Successfully sent reply {reply_idx}/{total_replies}: {message}')
        
        # Extract username from tweet URL
        username = tweet_url.split('/')[3]
        save_reply_id(username, tweet_url, reply_url)
        
        return True
    except Exception as e:
        error_msg = str(e).lower()
        # Handle specific error cases
        if "duplicate" in error_msg:
            logger.warning(f'{idx}) Duplicate reply detected, skipping: {message}')
        elif "rate limit" in error_msg:
            logger.warning(f'{idx}) Rate limit hit, waiting longer before next attempt')
            await asyncio.sleep(30)  # Wait longer for rate limits
        else:
            logger.error(f'{idx}) Failed to send reply {reply_idx}: {str(e)}')
        return False


async def check_account(account_data: Tuple[int, Tuple[str, str, str]], replies: list, target_url: str):
    idx, (evm_wallet, proxy, twitter_token, _) = account_data
    address = EthAccount().from_key(evm_wallet).address
    logger.info(f'{idx}) Processing {address}')

    account_info = AccountInfo(evm_address=address, proxy=proxy, twitter_auth_token=twitter_token)

    if '|' in account_info.proxy:
        change_link = account_info.proxy.split('|')[1]
        await change_ip(change_link)
        logger.info(f'{idx}) Successfully changed ip')

    twitter = Twitter(account_info)
    await twitter.start()

    await twitter.follow('elonmusk')
    
    # Reply to the target tweet with multiple messages
    for reply_idx, reply_message in enumerate(replies, 1):
        # Random delay between 5-10 seconds
        delay = random.uniform(5, 10)
        
        # Try to send the reply
        success = await safe_reply(twitter, target_url, reply_message, idx, reply_idx, len(replies))
        
        # Continue to next reply regardless of success/failure
        if reply_idx < len(replies):
            logger.info(f'{idx}) Waiting {delay:.1f} seconds before next reply...')
            await asyncio.sleep(delay)

    return True


async def process_batch(bid: int, batch, async_func, replies, target_url):
    failed = []
    for idx, d in enumerate(batch):
        try:
            await async_func(d, replies, target_url)
        except Exception as e:
            e_msg = str(e)
            if 'Could not authenticate you' in e_msg or 'account is suspended' in e_msg \
                    or 'account has been locked' in e_msg or 'account is temporarily locked' in e_msg:
                failed.append(d)
            await log_long_exc(d[0], 'Process account error', e)
    return failed


async def process(batches, async_func, replies, target_url):
    tasks = []
    for idx, b in enumerate(batches):
        tasks.append(asyncio.create_task(process_batch(idx, b, async_func, replies, target_url)))
    return await asyncio.gather(*tasks)


def main():
    # Ensure results directory exists
    os.makedirs('results', exist_ok=True)
    
    # Get target URL from user
    print("\nEnter the target tweet URL to reply to:")
    target_url = input("> ").strip()
    
    if not target_url:
        logger.error("No target URL provided. Exiting...")
        return
        
    if not ("https://x.com/" in target_url or "https://twitter.com/" in target_url):
        logger.error("Invalid Twitter/X URL format. Please use a valid URL.")
        return

    # Load reply messages
    replies = load_replies()
    logger.info(f'Loaded {len(replies)} reply messages from files/reply.txt')

    with open('files/evm_wallets.txt', 'r', encoding='utf-8') as file:
        evm_wallets = file.read().splitlines()
        evm_wallets = [w.strip() for w in evm_wallets]
    with open('files/proxies.txt', 'r', encoding='utf-8') as file:
        proxies = file.read().splitlines()
        proxies = [p.strip() for p in proxies]
        proxies = [p if '://' in p.split('|')[0] else 'http://' + p for p in proxies]
    with open('files/twitters.txt', 'r', encoding='utf-8') as file:
        twitters = file.read().splitlines()
        twitters = [t.strip() for t in twitters]
    with open('files/emails.txt', 'r', encoding='utf-8') as file:
        emails = file.read().splitlines()
        emails = [e.strip() for e in emails]

    if len(evm_wallets) != len(proxies):
        logger.error('Proxies count does not match wallets count')
        return
    if len(evm_wallets) != len(twitters):
        logger.error('Twitter count does not match wallets count')
        return
    if len(evm_wallets) != len(emails):
        logger.error('Emails count does not match wallets count')
        return

    def get_batches(threads: int = THREADS_NUM):
        _data = list(enumerate(list(zip(evm_wallets, proxies, twitters, emails)), start=1))
        _batches = [[] for _ in range(threads)]
        for _idx, d in enumerate(_data):
            _batches[_idx % threads].append(d)
        return _batches

    # Initialize event loop and run
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    results = loop.run_until_complete(process(get_batches(), check_account, replies, target_url))

    failed_twitter = set()
    for result in results:
        for r in result:
            failed_twitter.add(r[1][2])

    storage = AccountStorage('storage/data.json')
    storage.init()

    failed_cnt = 0

    print()

    open('results/working_evm_wallets.txt', 'w', encoding='utf-8').close()
    open('results/working_proxies.txt', 'w', encoding='utf-8').close()
    open('results/working_twitters.txt', 'w', encoding='utf-8').close()
    open('results/working_emails.txt', 'w', encoding='utf-8').close()
    for evm_wallet, proxy, twitter, email in zip(evm_wallets, proxies, twitters, emails):
        if twitter in failed_twitter:
            failed_cnt += 1
            address = EthAccount().from_key(evm_wallet).address
            logger.info(f'Removed for EVM address {address} twitter token {twitter}, proxy {proxy}')
            if CHECKER_UPDATE_STORAGE:
                storage.remove(address)
            continue
        with open('results/working_evm_wallets.txt', 'a', encoding='utf-8') as file:
            file.write(f'{evm_wallet}\n')
        with open('results/working_proxies.txt', 'a', encoding='utf-8') as file:
            file.write(f'{proxy}\n')
        with open('results/working_twitters.txt', 'a', encoding='utf-8') as file:
            file.write(f'{twitter}\n')
        with open('results/working_emails.txt', 'a', encoding='utf-8') as file:
            file.write(f'{email}\n')

    logger.info(f'Total failed count: {failed_cnt}')

    if CHECKER_UPDATE_STORAGE:
        storage.save()

    print()
    logger.info(f"Reply IDs have been saved to results/replyid.txt")
    print()


if __name__ == '__main__':
    cprint('###############################################################', 'cyan')
    cprint('#################', 'cyan', end='')
    cprint(' https://t.me/thelaziestcoder ', 'magenta', end='')
    cprint('################', 'cyan')
    cprint('#################', 'cyan', end='')
    cprint(' https://t.me/thelaziestcoder ', 'magenta', end='')
    cprint('################', 'cyan')
    cprint('#################', 'cyan', end='')
    cprint(' https://t.me/thelaziestcoder ', 'magenta', end='')
    cprint('################', 'cyan')
    cprint('###############################################################\n', 'cyan')
    main()
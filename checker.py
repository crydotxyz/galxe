import aiohttp
import asyncio
import random
import os
import time
import uuid
from datetime import datetime

from termcolor import cprint
from loguru import logger
from typing import Tuple, Optional

from internal.models import AccountInfo
from internal.twitter import Twitter
from internal.config import THREADS_NUM
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
        return []


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


async def check_account(account_data: Tuple[int, Tuple[str, str]],
                        replies: list,
                        target_url: str,
                        do_follow: bool,
                        do_retweet: bool,
                        do_like: bool):
    idx, (proxy, twitter_token) = account_data
    logger.info(f'{idx}) Processing twitter {twitter_token[:20]}...')

    account_info = AccountInfo(proxy=proxy, twitter_auth_token=twitter_token)

    if '|' in account_info.proxy:
        change_link = account_info.proxy.split('|')[1]
        await change_ip(change_link)
        logger.info(f'{idx}) Successfully changed ip')

    twitter = Twitter(account_info)
    await twitter.start()

    tweet_id = twitter.extract_tweet_id_from_url(target_url)
    username = target_url.split('/')[3]

    if do_follow:
        try:
            await twitter.follow(username)
            logger.info(f'{idx}) Successfully followed {username}')
        except Exception as e:
            logger.error(f'{idx}) Failed to follow {username}: {str(e)}')

    if do_like:
        try:
            await twitter.like(tweet_id)
            logger.info(f'{idx}) Successfully liked tweet {tweet_id}')
        except Exception as e:
            logger.error(f'{idx}) Failed to like tweet {tweet_id}: {str(e)}')

    if do_retweet:
        try:
            await twitter.retweet(tweet_id)
            logger.info(f'{idx}) Successfully retweeted tweet {tweet_id}')
        except Exception as e:
            logger.error(f'{idx}) Failed to retweet tweet {tweet_id}: {str(e)}')

    if replies:
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


async def process_batch(bid: int, batch, async_func, *args):
    failed = []
    for idx, d in enumerate(batch):
        try:
            await async_func(d, *args)
        except Exception as e:
            e_msg = str(e)
            if 'Could not authenticate you' in e_msg or 'account is suspended' in e_msg \
                    or 'account has been locked' in e_msg or 'account is temporarily locked' in e_msg:
                failed.append(d)
            await log_long_exc(d[0], 'Process account error', e)
    return failed


async def process(batches, async_func, *args):
    tasks = []
    for idx, b in enumerate(batches):
        tasks.append(asyncio.create_task(process_batch(idx, b, async_func, *args)))
    return await asyncio.gather(*tasks)


def parse_reply_ids_from_file(target_username: str) -> dict:
    """Parses replyid.txt to find replies for a specific user."""
    replies_to_delete = {}
    try:
        with open('results/replyid.txt', 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines()]
    except FileNotFoundError:
        logger.error("results/replyid.txt not found.")
        return {}

    for i, line in enumerate(lines):
        if line.startswith(f"Target (@{target_username})"):
            if i + 1 < len(lines) and lines[i+1].startswith("Reply:"):
                reply_line = lines[i+1]
                reply_url = reply_line.replace("Reply:", "").strip()
                try:
                    parts = reply_url.split('/')
                    replier_username = parts[3]
                    tweet_id = parts[5].split('?')[0]
                    if replier_username not in replies_to_delete:
                        replies_to_delete[replier_username] = []
                    if tweet_id not in replies_to_delete[replier_username]:
                        replies_to_delete[replier_username].append(tweet_id)
                except IndexError:
                    logger.warning(f"Could not parse reply URL: {reply_url}")
    return replies_to_delete


async def delete_reply_for_account(account_data, replies_to_delete: dict):
    idx, (proxy, twitter_token) = account_data

    account_info = AccountInfo(proxy=proxy, twitter_auth_token=twitter_token)
    twitter = Twitter(account_info)
    try:
        await twitter.start()
    except Exception as e:
        logger.error(f"{idx}) Failed to start twitter for {twitter_token[:20]}: {e}")
        return

    if twitter.my_username in replies_to_delete:
        tweet_ids = replies_to_delete[twitter.my_username]
        logger.info(f"{idx}) User {twitter.my_username} has {len(tweet_ids)} repl"
                    f"ies to delete.")
        for tweet_id in tweet_ids:
            try:
                await twitter.delete_tweet(tweet_id)
                logger.success(f"{idx}) Deleted reply {tweet_id} for user {twitter.my_username}")
                await asyncio.sleep(random.uniform(2, 5))
            except Exception as e:
                logger.error(f"{idx}) Failed to delete reply {tweet_id} for user {twitter.my_username}: {e}")


def delete_replies_main():
    cprint('\n' + '='*50, 'cyan')
    cprint("--- Delete Replies ---", 'white', attrs=['bold'])
    cprint('='*50, 'cyan')
    cprint("Enter the target username whose replies you want to delete (without @):", 'cyan')
    target_username = input("> ").strip()
    if not target_username:
        logger.error("No target username provided. Exiting.")
        return

    replies_to_delete = parse_reply_ids_from_file(target_username)
    if not replies_to_delete:
        logger.warning(f"No replies found for target user '{target_username}' in results/replyid.txt")
        return

    total_replies = sum(len(ids) for ids in replies_to_delete.values())
    logger.info(f"Found {total_replies} replies from {len(replies_to_delete)} users to delete.")

    # Load accounts
    try:
        with open('files/proxies.txt', 'r', encoding='utf-8') as file:
            proxies = [p.strip() for p in file.read().splitlines() if p.strip()]
        with open('files/twitters.txt', 'r', encoding='utf-8') as file:
            twitters = [t.strip() for t in file.read().splitlines() if t.strip()]
    except FileNotFoundError as e:
        logger.error(f"Could not open file: {e.filename}. Please make sure it exists.")
        return

    if not proxies:
        logger.error("'files/proxies.txt' is empty. Please add your proxies.")
        return
    if not twitters:
        logger.error("'files/twitters.txt' is empty. Please add your twitter tokens.")
        return

    def get_batches(threads: Optional[int] = THREADS_NUM):
        threads = threads if threads is not None else 1
        _data = list(enumerate(list(zip(proxies, twitters)), start=1))
        _batches = [[] for _ in range(threads)]
        for _idx, d in enumerate(_data):
            _batches[_idx % threads].append(d)
        return _batches

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(process(get_batches(), delete_reply_for_account, replies_to_delete))

    logger.info("Finished deleting replies.")


def reply_to_tweet_main():
    cprint('\n' + '='*50, 'cyan')
    cprint("--- Reply to Tweet ---", 'white', attrs=['bold'])
    cprint('='*50, 'cyan')
    cprint("\nEnter the target tweet URL:", 'cyan')
    target_url = input("> ").strip()

    if not target_url:
        logger.error("No target URL provided. Exiting...")
        return

    if not ("https://x.com/" in target_url or "https://twitter.com/" in target_url):
        logger.error("Invalid Twitter/X URL format. Please use a valid URL.")
        return

    # Ask for actions
    cprint("\nDo you want to like the tweet? (y/n)", 'cyan')
    like_choice = input("> ").strip().lower()
    do_like = like_choice == 'y'

    cprint("\nDo you want to retweet the tweet? (y/n)", 'cyan')
    retweet_choice = input("> ").strip().lower()
    do_retweet = retweet_choice == 'y'

    cprint("\nDo you want to follow the author? (y/n)", 'cyan')
    follow_choice = input("> ").strip().lower()
    do_follow = follow_choice == 'y'

    cprint("\nDo you want to reply to the tweet? (y/n)", 'cyan')
    reply_choice = input("> ").strip().lower()
    do_reply = reply_choice == 'y'

    replies = []
    if do_reply:
        replies = load_replies()
        if not replies:
            logger.error("Replying is enabled, but 'files/reply.txt' is either missing or empty. Please add your replies.")
            return
        logger.info(f'Loaded {len(replies)} reply messages from files/reply.txt')

    try:
        with open('files/proxies.txt', 'r', encoding='utf-8') as file:
            proxies = [p.strip() for p in file.read().splitlines() if p.strip()]
        with open('files/twitters.txt', 'r', encoding='utf-8') as file:
            twitters = [t.strip() for t in file.read().splitlines() if t.strip()]
    except FileNotFoundError as e:
        logger.error(f"File not found: {e.filename}. Please make sure it exists.")
        return

    if not proxies:
        logger.error("'files/proxies.txt' is empty. Please add your proxies.")
        return
    if not twitters:
        logger.error("'files/twitters.txt' is empty. Please add your twitter tokens.")
        return

    proxies = [p if '://' in p.split('|')[0] else 'http://' + p for p in proxies]

    if len(proxies) != len(twitters):
        logger.error('Twitter count does not match proxies count')
        return

    def get_batches(threads: Optional[int] = THREADS_NUM):
        threads = threads if threads is not None else 1
        _data = list(enumerate(list(zip(proxies, twitters)), start=1))
        _batches = [[] for _ in range(threads)]
        for _idx, d in enumerate(_data):
            _batches[_idx % threads].append(d)
        return _batches

    # Initialize event loop and run
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    results = loop.run_until_complete(process(get_batches(), check_account, replies, target_url,
                                              do_follow, do_retweet, do_like))

    failed_twitter = set()
    for result in results:
        for r in result:
            failed_twitter.add(r[1][1])

    failed_cnt = 0

    print()

    open('results/working_proxies.txt', 'w', encoding='utf-8').close()
    open('results/working_twitters.txt', 'w', encoding='utf-8').close()
    for proxy, twitter in zip(proxies, twitters):
        if twitter in failed_twitter:
            failed_cnt += 1
            logger.info(f'Removed twitter token {twitter}, proxy {proxy}')
            continue
        with open('results/working_proxies.txt', 'a', encoding='utf-8') as file:
            file.write(f'{proxy}\n')
        with open('results/working_twitters.txt', 'a', encoding='utf-8') as file:
            file.write(f'{twitter}\n')

    logger.info(f'Total failed count: {failed_cnt}')

    print()
    if do_reply:
        logger.info(f"Reply IDs have been saved to results/replyid.txt")
    print()


def main():
    # Ensure results directory exists
    os.makedirs('results', exist_ok=True)
    
    cprint('\n' + '='*50, 'cyan')
    cprint("--- Main Menu ---", 'white', attrs=['bold'])
    cprint('='*50, 'cyan')
    cprint("1. Reply to a tweet (and like/retweet/follow)", 'cyan')
    cprint("2. Delete replies for a target user saved in replyid.txt", 'cyan')
    cprint("\nSelect an action:", 'cyan')
    choice = input("> ").strip()

    if choice == '1':
        reply_to_tweet_main()
    elif choice == '2':
        delete_replies_main()
    else:
        logger.error("Invalid choice. Please enter 1 or 2.")


if __name__ == '__main__':
    cprint('###############################################################', 'cyan')
    cprint('#################### Twitter Automator ######################', 'cyan')
    cprint('###############################################################\n', 'cyan')
    main()
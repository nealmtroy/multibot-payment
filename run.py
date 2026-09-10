#!/usr/bin/env python3
import logging
import vip_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
    vip_bot.run()

import logging
import sys

from app.config import load_config
from app.menu_bar import StockMenuBarApp


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    config = load_config()
    app = StockMenuBarApp(config)
    app.run()


if __name__ == "__main__":
    main()

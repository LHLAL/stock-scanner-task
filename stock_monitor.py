import logging
from logging.handlers import RotatingFileHandler

from app.config import load_config
from app.menu_bar import StockMenuBarApp


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(
                "stock-monitor.log",
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,              # keep 5 backups
            ),
        ],
    )
    config = load_config()
    app = StockMenuBarApp(config)
    app.run()


if __name__ == "__main__":
    main()

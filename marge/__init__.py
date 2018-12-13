import logging as log
import coloredlogs

log.basicConfig(
    level=log.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
coloredlogs.install(level='INFO')
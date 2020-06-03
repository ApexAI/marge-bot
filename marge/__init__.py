import logging as log
import coloredlogs

log.basicConfig(
    level=log.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
coloredlogs.install(level='DEBUG')

# -*- coding: utf-8 -*-
"""
@File: main.py
@Author: jigangwang
@Email: wjigang@grupotr.es
@Date: 2026-07-28
@Desc: 
"""

from logger_utils.logger_manager import logger

def add(x:int, y:int) -> int:
    """_summary_

    Args:
        x (int): _description_
        y (int): _description_

    Returns:
        int: _description_
    """
    try:
        pass
        1 / 0
    except Exception as e:
        logger.error(f"Error: {e}")
        
if __name__ == '__main__':
    res = add(1, 2)
    logger.info("test logger")

# TheELNConsortium/any2eln
# © 2024 Nicolas CARPi @ Deltablot
# License MIT
import os
from getpass import getpass

def env_or_ask(envname: str, prompt: str) -> str:
    """Use env variable if it is set or ask the user"""
    res = os.getenv(envname)
    if res is not None:
        return res
    if envname == 'LABFOLDER_PASSWORD':
        return getpass(prompt)

    return input(prompt)


def debug(line: str) -> None:
    """Only print something if DEV=1 in env"""
    if os.getenv('DEV') == '1':
        print(line)

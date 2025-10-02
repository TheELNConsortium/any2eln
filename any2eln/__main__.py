# TheELNConsortium/any2eln
# © 2024 Nicolas CARPi @ Deltablot
# License MIT
import argparse
import os

from any2eln.labfolder.labfolder import Labfolder
from any2eln.utils.utils import env_or_ask


def main():
    sources = ['labfolder', 'labguru', 'scinote', 'benchling']
    parser = argparse.ArgumentParser(description='any2eln')
    parser.add_argument('--src', required=True, help='source service you want to export from', choices=sources)
    parser.add_argument('--out_dir', required=False, help='output directory', default='.')
    args = parser.parse_args()

    if args.src == 'labfolder':
        server = os.getenv('LABFOLDER_SERVER', 'labfolder.labforward.app')
        username = env_or_ask('LABFOLDER_USERNAME', 'Your Labfolder username or email: ')
        password = env_or_ask('LABFOLDER_PASSWORD', 'Your Labfolder password: ')
        lf = Labfolder(server, username, password, out_dir=args.out_dir)
        lf.extract()
    else:
        print('Not implemented.')


if __name__ == "__main__":
    main()

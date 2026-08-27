# TheELNConsortium/any2eln
# © 2024 Nicolas CARPi @ Deltablot
# License MIT
import argparse
import os

from any2eln.labfolder.json_export import LabfolderJson
from any2eln.labfolder.labfolder import Labfolder
from any2eln.utils.utils import env_or_ask


def main():
    sources = ['labfolder', 'labfolder-json', 'labguru', 'scinote', 'benchling']
    parser = argparse.ArgumentParser(description='any2eln')
    parser.add_argument('--src', required=True, help='source service you want to export from', choices=sources)
    parser.add_argument('--out_dir', required=False, help='output directory', default='.')
    parser.add_argument('--input', help='input JSON file for sources that read local data')
    parser.add_argument('--assets_dir', help='base directory used to resolve files referenced by the JSON export')
    parser.add_argument('--timezone', help='IANA timezone of Labfolder timestamps, for example Europe/Paris')
    parser.add_argument('--category_color', default='#29aeb9', help='color assigned to imported categories')
    parser.add_argument(
        '--entry_authors_file',
        help='JSON file mapping Labfolder entry IDs to author information',
    )
    args = parser.parse_args()

    if args.src == 'labfolder':
        server = os.getenv('LABFOLDER_SERVER', 'eln.labfolder.com')
        username = env_or_ask('LABFOLDER_USERNAME', 'Your Labfolder username or email: ')
        password = env_or_ask('LABFOLDER_PASSWORD', 'Your Labfolder password: ')
        lf = Labfolder(server, username, password, out_dir=args.out_dir)
        lf.extract()
    elif args.src == 'labfolder-json':
        if not args.input:
            parser.error('--input is required with --src labfolder-json')
        lf = LabfolderJson(
            args.input,
            out_dir=args.out_dir,
            assets_dir=args.assets_dir,
            timezone_name=args.timezone,
            category_color=args.category_color,
            entry_authors_file=args.entry_authors_file,
        )
        lf.extract()
    else:
        print('Not implemented.')


if __name__ == "__main__":
    main()

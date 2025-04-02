# TheELNConsortium/any2eln
# © 2024 Nicolas CARPi @ Deltablot
# License MIT

# labfolder module to extract all data from an account into a .eln
# use the extract() method

import hashlib
import shutil
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import random
from typing import Any
from itertools import groupby

import requests
from tqdm import tqdm
import pandas as pd
from typing_extensions import TypedDict
from any2eln.utils.utils import debug
from any2eln.utils.rocrate import get_crate_metadata


class Labfolder:
    def __init__(self, server: str, username: str, password: str, out_dir='.'):
        self.server = server
        # TODO: check for empty server
        self.username = username
        self.password = password
        self.token = self.__get_token()
        # number of entries to get in a request
        self.chunk_size = 100
        # output directory
        self.out_dir = out_dir
        self.categories = []

    def __get_token(self):
        """Generate a token. See https://labfolder.labforward.app/api/v2/docs/development.html#access-endpoints"""
        # allow skipping the request for the token if it's set in env
        if os.getenv('LABFOLDER_TOKEN') is not None:
            return os.getenv('LABFOLDER_TOKEN')

        # token is not present in env, so get it from auth/login api
        url = "https://" + self.server + "/api/v2/auth/login"
        headers = {'Content-Type': 'application/json'}
        data = {
            'user': self.username,
            'password': self.password,
        }
        try:
            response = requests.post(url, headers=headers, data=json.dumps(data))
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f'Error getting token: {e}')
            sys.exit(1)
        return response.json()['token']

    def __get_entries(self):
        # we first make a limited query to get the total number of entries
        first = self.__get_entries_chunk(0, 1)
        # it is stored in the x-total-count response header
        total_count = int(first.headers.get('x-total-count'))
        print(f'Found {total_count} entries')
        chunk_number = total_count // self.chunk_size
        debug(f'Number of chunks: {chunk_number}')
        entries = []
        for i in range(chunk_number + 1):
            offset = i * self.chunk_size
            entries.extend(self.__get_entries_chunk(offset).json())
        return entries

    def __get_entries_chunk(self, offset: int, limit=100):
        url = 'https://' + self.server + '/api/v2/entries'
        headers = {'Authorization': f'Bearer {self.token}'}
        params = {'expand': 'author,project,last_editor', 'limit': limit, 'offset': offset}
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f'Error getting entries: {e}')
            sys.exit(1)
        return response

    def __get_unique_enough_id(self) -> str:
        return random.randbytes(20).hex()

    def __get_node_from_csv(self, csv_id: str, csv: tuple[str, str], entry_folder: Path):
        node: dict[str, Any] = {}
        node['@id'] = f"./{entry_folder.name}/{csv_id}"
        node['@type'] = 'File'
        node['name'] = csv[0]
        contentSize = len(csv[1].encode())
        if contentSize > 0:
            node['contentSize'] = contentSize
        node['encodingFormat'] = 'text/csv'
        return node

    def __get_node_from_metadata(self, json: dict[str, Any], entry_folder: Path):
        node: dict[str, Any] = {}
        node['@id'] = f"./{entry_folder.name}/{json.get('id')}"
        node['@type'] = 'File'
        node['name'] = json.get('file_name') or json.get('title') or 'Unknown'
        contentSize = int(json.get('file_size', 0))
        if contentSize > 0:
            node['contentSize'] = contentSize
        node['encodingFormat'] = json.get('content_type') or json.get('original_file_content_type')
        return node

    def extract(self) -> Path:
        if os.getenv('USE_LOCAL') == '1':
            local_file = 'entries.json'
            print(f'Using local file: {local_file}')
            with open('entries.json', 'r') as file:
                entries = json.load(file)
        else:
            entries = self.__get_entries()
            if os.getenv('SAVE_ENTRIES') == '1':
                with open('entries.json', 'w') as file:
                    json.dump(entries, file, indent=2)
        # we're going to split the entries based on the author_id value, and generate a .eln for each author
        sorted_entries = sorted(entries, key=lambda e: e['author_id'])
        entries_by_author = {key: list(group) for key, group in groupby(sorted_entries, key=lambda e: e['author_id'])}
        if os.getenv('SAVE_ENTRIES') == '1':
            with open('entries-sorted.json', 'w') as file:
                json.dump(entries_by_author, file, indent=2)

        # this will hold a little summary with the author id and their name and email
        summary = ''

        # define a type for the node with @id = ./
        SelfNode = TypedDict('SelfNode', {'@id': str, '@type': str, 'hasPart': list[dict[str, str]]})

        now = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        main_dir = Path(self.out_dir).joinpath(f'export-{now}')
        main_dir.mkdir()

        with tqdm(total=len(entries_by_author)) as superpbar:
            for author, author_entries in entries_by_author.items():
                debug(f'Processing author: {author}')

                crate_metadata = get_crate_metadata()
                # the @id = ./ node
                self_node: SelfNode = {'@id': './', '@type': 'Dataset', 'hasPart': []}

                author_dir = main_dir.joinpath(f'author-{author}')
                author_dir.mkdir()

                with tqdm(total=len(author_entries)) as pbar:
                    for entry in author_entries:
                        debug(f"Extracting entry with ID: {entry['id']}")
                        entry_folder = author_dir.joinpath(entry['id'])
                        entry_folder.mkdir()
                        content = []
                        files = []

                        for element in entry['elements']:
                            debug(f"Processing {element.get('type')} with ID: {element['id']}")
                            # see https://labfolder.labforward.app/api/v2/docs/development.html#entry-elements-file-elements-get
                            if element['type'] == 'FILE':
                                # get the json first so we can grab the name
                                res = self.__get_element(element)
                                if res is None:
                                    continue
                                json_metadata = res.json()
                                node = self.__get_node_from_metadata(json_metadata, entry_folder)
                                with entry_folder.joinpath(f"{element['id']}.json").open('w') as json_file:
                                    json.dump(json_metadata, json_file, indent=4)
                                # now get original data, the image itself
                                res = self.__get_element(element, True)
                                # might throw error 400 if element has nothing to download
                                if res is None:
                                    continue
                                with entry_folder.joinpath(element['id']).open('wb') as file:
                                    file.write(res.content)
                                node['sha256'] = hashlib.sha256(res.content).hexdigest()
                                crate_metadata['@graph'].append(node)
                                files.append(node['@id'])

                            if element['type'] == 'IMAGE':
                                # get the json first
                                res = self.__get_element(element)
                                if res is None:
                                    continue
                                json_metadata = res.json()
                                node = self.__get_node_from_metadata(json_metadata, entry_folder)
                                with entry_folder.joinpath(f"{element['id']}.json").open('w') as json_file:
                                    json.dump(json_metadata, json_file, indent=2)
                                # now get original data, the image itself
                                res = self.__get_element(element, True)
                                with entry_folder.joinpath(element['id']).open('wb') as file:
                                    file.write(res.content)
                                node['sha256'] = hashlib.sha256(res.content).hexdigest()
                                crate_metadata['@graph'].append(node)
                                files.append(node['@id'])

                            # for TABLE we try and save each sheet as a csv but also keep the full json around as a json file
                            # a WELL_PLATE has the same structure as a TABLE, so we can use the same code
                            if element['type'] == 'TABLE' or element['type'] == 'WELL_PLATE':
                                res = self.__get_element(element)
                                if res is None:
                                    continue
                                json_metadata = res.json()
                                node = self.__get_node_from_metadata(json_metadata, entry_folder)
                                # save the full json
                                with entry_folder.joinpath(f"{json_metadata['id']}").open('w') as json_file:
                                    json.dump(json_metadata, json_file, indent=2)
                                node['sha256'] = hashlib.sha256(
                                    json.dumps(json_metadata, indent=2).encode()
                                ).hexdigest()
                                crate_metadata['@graph'].append(node)
                                files.append(node['@id'])

                                # now save sheets as csv files
                                csvs = self.__get_csvs_from_json(json_metadata)
                                for csv in csvs:
                                    # get an id so we can store it without clashes
                                    csv_id = self.__get_unique_enough_id()
                                    node = self.__get_node_from_csv(csv_id, csv, entry_folder)
                                    with entry_folder.joinpath(csv_id).open('w') as file:
                                        file.write(csv[1])
                                    node['sha256'] = hashlib.sha256(csv[1].encode()).hexdigest()
                                    crate_metadata['@graph'].append(node)
                                    files.append(node['@id'])

                            # for this element type we simply store the json for now
                            if element['type'] == 'DATA':
                                res = self.__get_element(element)
                                if res is None:
                                    continue
                                json_metadata = res.json()
                                node = self.__get_node_from_metadata(json_metadata, entry_folder)
                                with entry_folder.joinpath(f"{json_metadata['id']}").open('w') as json_file:
                                    json.dump(json_metadata, json_file, indent=2)
                                node['sha256'] = hashlib.sha256(
                                    json.dumps(json_metadata, indent=2).encode()
                                ).hexdigest()
                                crate_metadata['@graph'].append(node)
                                files.append(node['@id'])

                            if element['type'] == 'TEXT':
                                res = self.__get_element(element)
                                if res is None:
                                    continue
                                json_metadata = res.json()
                                with entry_folder.joinpath(f"{json_metadata['id']}.json").open('w') as json_file:
                                    json.dump(json_metadata, json_file, indent=2)
                                content.append(json_metadata['content'])

                        pbar.update(1)

                        # create the Dataset node
                        crate_metadata['@graph'].append(self.__get_dataset_node(entry, content, files))
                        # add the author node only if it doesn't exist already
                        author_node = self.__get_author_node(entry)
                        if author_node not in crate_metadata['@graph']:
                            crate_metadata['@graph'].append(author_node)
                            summary += f"\n{author_node.get('@id')} | {author_node.get('familyName')} | {author_node.get('givenName')} | {author_node.get('email')}"
                        self_node['hasPart'].append({'@id': f"./{entry['id']}"})

                    # end loop on entries
                # end tqdm loop

                # add the self node now that it has all the hasPart
                crate_metadata['@graph'].append(self_node)

                with author_dir.joinpath('ro-crate-metadata.json').open('w') as json_file:
                    json.dump(crate_metadata, json_file, indent=2)

                # create a container directory because shutil will gobble up the folder
                # don't use joinpath here because the endslash of the author_dir is kept
                container = Path(f'{author_dir}-container')
                container.mkdir()
                author_dir.rename(container.joinpath(author_dir.name))
                eln_name = 'tmp.eln'
                shutil.make_archive(eln_name, 'zip', container)
                # shutil will add a .zip extension but we want a .eln
                Path(f'{eln_name}.zip').rename(f'{author_dir}.eln')
                print(f'Created {author_dir}.eln')
                superpbar.update(1)

        # write the summary.txt file with authors names
        with main_dir.joinpath('summary.txt').open('w') as file:
            file.write(summary)

        # create a script so we can create the Projects and then link experiments with same tag to them
        with main_dir.joinpath('create-projects.py').open('w') as file:
            file.write(self.__get_project_script())
        # create a sql script to make links from experiments to projects
        with main_dir.joinpath('create-links.sql').open('w') as file:
            file.write(self.__get_links_script())
        return main_dir

    def __get_links_script(self) -> str:
        lines = []
        for category in self.categories:
            lines.append(
                f"""
INSERT INTO experiments_links (item_id, link_id)
SELECT
  experiments.id,
  (SELECT items.id FROM items WHERE title = "{category}" LIMIT 1)
FROM experiments
LEFT JOIN tags2entity ON tags2entity.item_id = experiments.id AND tags2entity.item_type = 'experiments'
LEFT JOIN tags ON tags2entity.tag_id = tags.id
WHERE tags.tag = "{category}";"""
            )
        return '\n'.join(lines)

    def __get_project_script(self) -> str:
        header = """#!/usr/bin/env python
import elabapi_python
API_HOST_URL = 'https://elab.local:3148/api/v2'
API_KEY = 'apiKey4Test'
configuration = elabapi_python.Configuration()
configuration.api_key['api_key'] = API_KEY
configuration.api_key_prefix['api_key'] = 'Authorization'
configuration.host = API_HOST_URL
configuration.debug = False
configuration.verify_ssl = False
api_client = elabapi_python.ApiClient(configuration)
api_client.set_default_header(header_name='Authorization', header_value=API_KEY)
itemsTypesApi = elabapi_python.ItemsTypesApi(api_client)
response = itemsTypesApi.post_items_types_with_http_info(body={'title': "Projects"})
locationHeaderInResponse = response[2].get('Location')
projects_cat_id = int(locationHeaderInResponse.split('=').pop())
itemsApi = elabapi_python.ItemsApi(api_client)
"""

        body = map(self.__to_post_action, self.categories)
        return header + '\n'.join(body)

    def __to_post_action(self, category: str) -> str:
        s = """
response = itemsApi.post_item_with_http_info(body={'category_id': projects_cat_id})
locationHeaderInResponse = response[2].get('Location')
itemId = int(locationHeaderInResponse.split('/').pop())
itemsApi.patch_item(itemId, body={'title': """
        return f'{s}"{category}"' + '})'

    def __get_dataset_node(self, entry: dict[str, Any], content: list[str], files: list[str]):
        node: dict[str, Any] = {}
        node['@id'] = f"./{entry['id']}"
        node['@type'] = 'Dataset'
        node['name'] = entry['title']
        node['author'] = {'@id': f"author://{entry.get('author_id')}"}
        node['text'] = ''.join(content)
        project_title = entry.get('project', {}).get('title', {})
        # add the project title to the list of tags
        entry['tags'].append(project_title)
        # and store it in our general list of projects
        if project_title not in self.categories:
            self.categories.append(project_title)
        node['keywords'] = ','.join(entry.get('tags', []))
        # use this to create a Category with the Project title
        # node['category'] = entry.get('project', {}).get('title', {})
        node['dateCreated'] = entry['creation_date']
        node['dateModified'] = entry['version_date']
        node['hasPart'] = [{'@id': file} for file in files]
        return node

    def __get_author_node(self, entry: dict[str, Any]):
        node: dict[str, Any] = {}
        author_node = entry.get('author', {})
        node['@id'] = f"author://{author_node.get('id')}"
        node['@type'] = 'Person'
        node['familyName'] = author_node['last_name']
        node['givenName'] = author_node['first_name']
        node['email'] = author_node['email']
        return node

    def __get_element(self, element: dict[str, Any], get_data=False):
        # the replace() is present for WELL_PLATE -> well-plate
        url = f"https://{self.server}/api/v2/elements/{element['type'].lower().replace('_', '-')}/{element['id']}"
        # for images we want to download the image
        if element['type'] == 'IMAGE' and get_data == True:
            url += '/original-data'
        if element['type'] == 'FILE' and get_data == True:
            url += '/download'
        debug(f'GET {url}')
        debug(f'curl -v -H "Authorization: Bearer $LABFOLDER_TOKEN" {url}')
        debug('')
        headers = {'Authorization': f'Bearer {self.token}'}
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f'Error getting element: {e}')
            debug('Skipping element that was not found:')
            debug(json.dumps(element, indent=2))
            return None
        return response

    def __get_csvs_from_json(self, json_metadata: dict[str, Any]) -> list[tuple[str, str]]:
        """Transform a table from json to DataFrame"""
        csvs: list[tuple[str, str]] = []
        # for some reason sometimes Sheets are not present
        sheets = json_metadata['content'].get('sheets', None)
        if sheets is None:
            debug('Skipping csv that has no sheets in content:')
            debug(json.dumps(json_metadata, indent=2))
            return csvs
        # we process all the sheets, it will result in a different csv each
        for sheet_name, sheet_val in sheets.items():
            table_data = sheet_val['data'].get('dataTable', None)
            if table_data is None:
                debug('Skipping csv that has no dataTable in data:')
                debug(json.dumps(json_metadata, indent=2))
                continue
            # column_names = [str(table_data['0'][str(i)].get('value', '')) for i in range(len(table_data['0']))]
            rows = []
            for key, values in table_data.items():
                row = {}
                for col_key, col_data in values.items():
                    row[col_key] = col_data.get('value', 'N/A')
                rows.append(row)

            # Create DataFrame from the list of dictionaries
            df = pd.DataFrame(rows)
            # drop first row
            # df = df.drop(0)
            # None as first arg will return the csv as string instead of writing a file
            csvs.append((sheet_name + '.csv', df.to_csv(None, index=False)))
        return csvs

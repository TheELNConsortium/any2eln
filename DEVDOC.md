# Developer documentation

## Run locally

### Install

~~~bash
git clone https://gitub.com/TheELNConsortium/any2eln
cd any2eln
uv sync
~~~

### Usage

~~~bash
source local.env
uv run -m any2eln
~~~

### Api doc

https://labfolder.labforward.app/api/v2/docs/development.html

### Using curl

First, login to get a TOKEN

~~~bash
curl -X POST -H 'Content-Type: application/json' -d '{"user":"example@example.com", "password": "secr3t"}' https://labfolder.labforward.app/api/v2/auth/login
~~~

and copy token in some env var like T. Then:

~~~bash
curl -H "Authorization: Bearer $T" "https://labfolder.labforward.app/api/v2/templates/26333?expand=entry"
~~~

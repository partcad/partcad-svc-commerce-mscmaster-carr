# McMaster-Carr for PartCAD

McMaster-Carr, published with the other stores as `//pub/svc/commerce/mcmaster`, in two parts:

* **a store**, `//pub/svc/commerce/mcmaster:mcmaster`, that quotes McMaster-Carr part numbers
  at McMaster-Carr's prices, in US dollars, by the pack where McMaster-Carr sells packs;
* **a catalogue**, `//pub/svc/commerce/mcmaster/products`: a McMaster-Carr part by its number
  (`products:91290A115`), with the 3D STEP model McMaster-Carr publishes of it, its
  specifications in its description, and `vendor: mcmaster`, `sku` and `count_per_sku` (the
  pack) to buy it by.

Both are one script, `mcmaster.py`, asking McMaster-Carr's **Product Information API**
(<https://www.mcmaster.com/help/api/>). Of everything evaluated, it is the one with a real
STEP model for almost every part, served over the API itself.

## Setting it up

The API is for McMaster-Carr's **approved customers**: ask `eProcurement@mcmaster.com` for
access, and McMaster-Carr issues a client certificate and its password, for an account enabled
for the API. Then:

```shell
pc login //pub/svc/commerce/mcmaster:mcmaster_repo
pc login //pub/svc/commerce/mcmaster:mcmaster
```

with the account's user name and password, the certificate - the path of the `.pfx`/`.p12`
(or `.pem`) file, or its content in base64 where the plugin runs in a container that cannot see
the file - and the certificate's password. `MCMASTER_USERNAME`, `MCMASTER_PASSWORD`,
`MCMASTER_CERTIFICATE` and `MCMASTER_CERTIFICATE_PASSWORD` work too. A PKCS#12 certificate is
read with `cryptography`, which the package's `pythonRequirements` puts in the sandbox.

## Using it

```shell
pc inspect //pub/svc/commerce/mcmaster/products:91290A115
pc search parts -P //pub/svc/commerce/mcmaster/products -k "91290A115 92095A181"
pc supply quote //pub/svc/commerce/mcmaster/products:91290A115#250
```

## How McMaster-Carr is asked

| What PartCAD asks | McMaster-Carr API |
| --- | --- |
| a token | `POST /v1/login`, with the certificate; kept for its day |
| a part | `PUT /v1/products {"URL": "https://mcmaster.com/<number>"}`: subscribing answers the part's information |
| its price | `GET /v1/products/<number>/price`: `[{Amount, MinimumQuantity, UnitOfMeasure}]` |
| its model | `GET /v1/cad/...`, the `3-D ...` entries of its `Links` (a `2-D` drawing is not a model) |

## What it cannot do (yet)

* **No search and no categories.** The API looks up a part number and nothing else, so the
  catalogue is one package: a search answers the part numbers in what is searched for
  (`-k "91290A115 92095A181"`), and anything else finds nothing.
* **Reading a part subscribes the account to it**, and McMaster-Carr caps an account's
  subscriptions, in all and per day, without saying how many. A part is read once a day at most.
* **No stock, lead time or ordering** in the API: McMaster-Carr is ordered from through
  punch-out (cXML) or EDI, set up with McMaster-Carr. A pack's size is read from the price's
  unit ("Pack of 100") or the description, which is McMaster-Carr's wording and not a field.
* **McMaster-Carr's terms are yours to keep**: what the API answers is for use within your
  company, and its CAD models are to help decide whether to buy, not to be redistributed. The
  plugin puts a model nowhere but your own PartCAD cache; a package must never carry one.

## A mock server to try it against

McMaster-Carr's API is for its approved customers, so `mock_mcmaster.py` stands in for it:
an HTTPS server that answers the same requests the same way - login, subscribe, price, CAD,
McMaster-Carr's error codes - from a catalogue of its own (`mock/catalog.json`, with CAD files
of this repository's own drawing under `mock/cad/`), and that, like McMaster-Carr, refuses any
connection without a client certificate it trusts.

```shell
pip install cryptography
python mock_mcmaster.py            # https://127.0.0.1:8443; certificates in ./mock-certs
```

Its first run makes a certificate authority, the server's certificate, and a client
certificate as a PKCS#12 file with a password - what McMaster-Carr sends a customer - and it
prints the parameters to give the plugins, including `api` (its address) and `ca` (the
authority to trust it by), for `~/.partcad/config.yaml`:

```yaml
parameters:
  //pub/svc/commerce/mcmaster:mcmaster_repo:
    api: "https://127.0.0.1:8443"
    ca: ".../mock-certs/ca.pem"
    username: "partcad@example.com"
    password: "mock-password"
    certificate: ".../mock-certs/client.pfx"
    certificate_password: "mock-certificate"
  //pub/svc/commerce/mcmaster:mcmaster:
    # the same
```

and then everything above works against it: `pc inspect` builds `91290A115`, `94180A331` and
`6661K14`, `pc supply quote` prices them, `4936K451` is discontinued, and anything else is no
McMaster-Carr part. `test_mock_mcmaster.py` runs the plugin against it over TLS, with
certificates made for the test.

Not yet run against McMaster-Carr itself, for want of an approved account here: the mock
follows McMaster-Carr's documentation and its Postman examples.

## Tests

```shell
pip install pytest cryptography
pip install --no-deps partcad    # for plugin_models, which PartCAD gives every plugin
pytest
```

`distributor.py` is shared, byte for byte, with the other distributor packages (DigiKey,
Octopart); it is edited in `incubator/_shared/` of the PartCAD repository and copied here, and
`distributor.sha256` is its digest, which the tests hold this copy to.

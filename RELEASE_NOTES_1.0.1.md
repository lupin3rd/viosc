# viOSC 1.0.1

**Pairs with VJmix 1.3.1. Requires Vimix 0.9.3 or newer.**

- **Folder sessions.** `GET /fs/scan` walks a Media Roots folder (bounded) so
  VJmix can turn it into a session: a count, the opening clip and the recipe.
- **A smaller configuration.** 18 keys instead of 27: the internal timing and
  pairing-lock policy is fixed, pairing is always on, and the seven operator
  settings are edited from VJmix (the two bind fields stay in the viOSC window).
  Old `config.json` files migrate on load.
- **Dashboard.** The read-only snapshot lists every effective setting and never
  the pairing secret; a busy HTTP port is one short line instead of a clipped
  paragraph.

# Tabler

Vendored `@tabler/core` 1.7.3, MIT licensed. Only the compiled core stylesheet is required;
existing page JavaScript handles interactions. No CDN, build step or Bootstrap JavaScript.
Set flex direction explicitly for the horizontal navigation and page headers; Tabler's
generic navbar and page-header layouts also define flex geometry.

Source: https://registry.npmjs.org/@tabler/core/-/core-1.7.3.tgz
SHA-512 integrity: `sha512-9PDS6U69svI987zIpd0AVLF4Gw9Uxfq1aypceVMNxye9Jp4QWYZEtzTL7z60L8qLyVQupHA+ug1AIA8gGAnYRA==`

The stylesheet was extracted from the integrity-verified npm archive. Licence: upstream
https://github.com/tabler/tabler/blob/dev/LICENSE (the npm archive omits the root licence).
To update, replace the core CSS with an integrity-verified package release, retain the licence
and run dashboard tests plus static browser checks for all five pages.

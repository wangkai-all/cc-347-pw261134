"""annarchive — pointwise announcement archiver with provenance-locked versions.

Pipeline stages are deliberately independent:

1. body fetch  -> an *immutable* version snapshot (referenced URLs + declared
   checksums are frozen at lock time)
2. attachment downloads -> one provenance row per reference per version;
   blobs are content-addressed and may be shared, provenance may not
3. publish -> a version becomes readable only when every binding is verified

A correction observed mid-download can never mutate a locked version; it only
becomes visible on the next fetch, which creates a new version.
"""

__version__ = "1.0.0"

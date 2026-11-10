"""Names and markers inside a .hbk that are not part of the data pool: file revisions (``rev_of``), the share-root parent id,
the ransomware suffix and note patterns, and the extension sets behind the search flags."""
import re

ROOT_PARENT = "5058F1AF5058F1AF8388633F609CADB75A75DC9D"   # pname_id_v2 of share-root entries

RANSOM_SUFFIX = ".L0CK3D"
RANSOM_TRAILER = 588       # bytes the C3RB3R ransomware appends to every file it renames *.L0CK3D (issue #250); the rest stays in place

# A file name in a backup is <base>[.<revision>][.L0CK3D]. Cloud targets (Google Drive, C2) add the revision to every name; a
# local target (USB disk, a shared folder) has none (issue #250), which counts as revision 0. See rev_of and split_name.

RANSOM_NOTE = re.compile(r"^(read-me.*\.txt|.*how[_ -]to[_ -](decrypt|recover|restore).*|.*(your|all)[_ -]files.*\.(txt|html)|.*\.L0CK3D)$", re.I)

SCRIPT_EXT = {"sh","bash","zsh","py","pl","php","rb","ps1","bat","cmd","vbs","vbe","js","jse","wsf","hta","jar","elf",
              "bin","run","so","ko","dylib","exe","dll","scr","com","msi","apk","lnk","pyc","cgi","phtml","php5","phar","deb","rpm","spk"}

BENIGN_EXEC_EXT = {"jpg","jpeg","png","gif","heic","mov","mp4","mp3","pdf","docx","xlsx","pptx","txt","nef","raw","cr2","avi","m4a","m4v","zip","7z","rar"}

DOC_EXT = {"pdf","doc","docx","xls","xlsx","ppt","pptx","txt","rtf","odt","jpg","jpeg","png"}

def rev_of(name, base):
    """'version_info.db.2334' -> (2334, False); 'version_info.db' (a local target) -> (0, False); a ransomware-renamed
    'version_info.db.2334.L0CK3D' -> (2334, True). None when ``name`` is not ``base`` with a revision."""
    m = re.match(r"^" + re.escape(base) + r"(?:\.(\d+))?(" + re.escape(RANSOM_SUFFIX) + r")?$", name)
    return (int(m.group(1) or 0), bool(m.group(2))) if m else None

def split_name(name, pattern):
    """Match ``name`` as <pattern>[.<revision>][.L0CK3D]; ``pattern`` is a regex for the base with its own groups (e.g.
    r"(\\d+)\\.(bucket|index)"). Returns (groups of pattern, revision, locked) or None; a name without a revision is revision 0."""
    m = re.match(r"^(?:" + pattern + r")(?:\.(\d+))?(" + re.escape(RANSOM_SUFFIX) + r")?$", name)
    if not m: return None
    g = m.groups(); return g[:-2], int(g[-2] or 0), bool(g[-1])

def is_sqlite(p):
    try:
        with open(p, "rb") as f: return f.read(16) == b"SQLite format 3\x00"
    except OSError: return False

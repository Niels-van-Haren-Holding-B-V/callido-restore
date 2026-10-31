"""Names and markers inside a .hbk that are not part of the data pool: file revisions (``rev_of``), the share-root parent id,
the ransomware suffix and note patterns, and the extension sets behind the search flags."""
import re

ROOT_PARENT = "5058F1AF5058F1AF8388633F609CADB75A75DC9D"   # pname_id_v2 of share-root entries

RANSOM_SUFFIX = ".L0CK3D"

RANSOM_NOTE = re.compile(r"^(read-me.*\.txt|.*how[_ -]to[_ -](decrypt|recover|restore).*|.*(your|all)[_ -]files.*\.(txt|html)|.*\.L0CK3D)$", re.I)

SCRIPT_EXT = {"sh","bash","zsh","py","pl","php","rb","ps1","bat","cmd","vbs","vbe","js","jse","wsf","hta","jar","elf",
              "bin","run","so","ko","dylib","exe","dll","scr","com","msi","apk","lnk","pyc","cgi","phtml","php5","phar","deb","rpm","spk"}

BENIGN_EXEC_EXT = {"jpg","jpeg","png","gif","heic","mov","mp4","mp3","pdf","docx","xlsx","pptx","txt","nef","raw","cr2","avi","m4a","m4v","zip","7z","rar"}

DOC_EXT = {"pdf","doc","docx","xls","xlsx","ppt","pptx","txt","rtf","odt","jpg","jpeg","png"}

def rev_of(name, base):
    """'version_info.db.2334' -> 2334 ; also tolerates ransomware suffix."""
    m = re.match(r"^" + re.escape(base) + r"\.(\d+)(" + re.escape(RANSOM_SUFFIX) + r")?$", name)
    return (int(m.group(1)), bool(m.group(2))) if m else None

def is_sqlite(p):
    try:
        with open(p, "rb") as f: return f.read(16) == b"SQLite format 3\x00"
    except OSError: return False

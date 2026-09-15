"""
SolidWorks Connection Management
Handles connection to SolidWorks application and template management
"""

import win32com.client
import pythoncom
import glob
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


# swUserPreferenceStringValue_e values from the installed SOLIDWORKS API.
# These enum values are stable API identifiers, not localized UI strings.
_DEFAULT_TEMPLATE_PREFS = {
    "part": 8,      # swDefaultTemplatePart
    "assembly": 9,  # swDefaultTemplateAssembly
    "drawing": 10,  # swDefaultTemplateDrawing
}
_TEMPLATE_EXTENSIONS = {
    "part": ".prtdot",
    "assembly": ".asmdot",
    "drawing": ".drwdot",
}
_PREFERRED_TEMPLATE_NAMES = {
    "part": ("Part.prtdot", "gb_part.prtdot"),
    "assembly": ("Assembly.asmdot", "gb_assembly.asmdot"),
    "drawing": ("Drawing.drwdot",),
}


class SolidWorksConnection:
    """Manages connection to SolidWorks application"""
    
    def __init__(self):
        self.app = None
        self.template_path = None
        self.assembly_template_path = None
        self.drawing_template_path = None
        
    def connect(self) -> bool:
        """Connect to SolidWorks application"""
        try:
            # Try to connect to existing instance
            try:
                self.app = win32com.client.GetActiveObject("SldWorks.Application")
                logger.info("Connected to existing SolidWorks instance")
            except Exception:
                # Launch new instance and wait for it to be ready
                logger.info("SolidWorks not running, launching new instance...")
                self.app = win32com.client.Dispatch("SldWorks.Application")
                self.app.Visible = True
                for i in range(30):
                    try:
                        _ = self.app.RevisionNumber
                        logger.info("SolidWorks instance is ready")
                        break
                    except Exception:
                        logger.info(f"Waiting for SolidWorks to start ({i+1}/30)...")
                        time.sleep(1)
                else:
                    raise Exception("SolidWorks failed to start within 30 seconds")

            version = self.app.RevisionNumber
            logger.info(f"SolidWorks version: {version}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to SolidWorks: {e}")
            self.app = None
            return False
    
    def _sw_year(self) -> Optional[int]:
        """Derive the SolidWorks release year from the COM revision number.
        Revision major 33 = SW2025 (year = 1992 + major)."""
        try:
            major = int(str(self.app.RevisionNumber).split(".")[0])
            return 1992 + major
        except Exception:
            return None

    def _pick_template(self, patterns) -> Optional[str]:
        """Glob for a template, preferring the one matching the running SW version.
        Machines with multiple SW versions installed have templates for each year."""
        hits = []
        for pattern in patterns:
            hits.extend(glob.glob(pattern))
        if not hits:
            return None
        year = self._sw_year()
        if year:
            for hit in hits:
                if str(year) in hit:
                    return hit
        return hits[0]

    def _user_default_template(self, kind: str) -> Optional[str]:
        """Return a usable SOLIDWORKS default template preference.

        Some installations return virtual placeholders such as
        ``~BLANK_PART_TEMPLATE.prtdot``.  NewDocument cannot consume those as
        filesystem paths, so only an existing file with the expected suffix
        is accepted here.
        """
        if not self.app:
            return None
        try:
            path = str(self.app.GetUserPreferenceStringValue(
                _DEFAULT_TEMPLATE_PREFS[kind]
            ) or "").strip()
        except Exception as exc:
            logger.info("Could not read the %s default template: %s", kind, exc)
            return None

        extension = _TEMPLATE_EXTENSIONS[kind]
        if path and path.lower().endswith(extension) and os.path.isfile(path):
            logger.info("Found %s template from SOLIDWORKS preferences: %s", kind, path)
            return path
        if path:
            logger.info("Ignoring unusable %s default template preference: %s", kind, path)
        return None

    def _template_directories(self) -> list[str]:
        """Return installed template directories, current SW version first."""
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        pattern = os.path.join(
            program_data, "SOLIDWORKS", "SOLIDWORKS *", "templates"
        )
        roots = [p for p in glob.glob(pattern) if os.path.isdir(p)]
        year = self._sw_year()
        roots.sort(key=lambda p: (0 if year and str(year) in p else 1, p.casefold()))
        return roots

    def _scan_template(self, kind: str) -> Optional[str]:
        """Find a normal document template, falling back to MBD last."""
        extension = _TEMPLATE_EXTENSIONS[kind]
        roots = self._template_directories()

        # Prefer conventional names in each version's root directory, then any
        # other root-level template.  This deliberately avoids MBD templates.
        for root in roots:
            root_files = sorted(
                (p for p in glob.glob(os.path.join(root, f"*{extension}"))
                 if os.path.isfile(p)),
                key=str.casefold,
            )
            by_name = {os.path.basename(p).casefold(): p for p in root_files}
            for preferred in _PREFERRED_TEMPLATE_NAMES[kind]:
                hit = by_name.get(preferred.casefold())
                if hit:
                    logger.info("Found %s template in version root: %s", kind, hit)
                    return hit
            if root_files:
                logger.info("Found %s template in version root: %s", kind, root_files[0])
                return root_files[0]

        # MBD templates carry size-specific semantics, so use one only when no
        # ordinary root template exists in any installed version.
        for root in roots:
            mbd_files = sorted(
                (p for p in glob.glob(os.path.join(root, "MBD", "**", f"*{extension}"),
                                      recursive=True)
                 if os.path.isfile(p)),
                key=str.casefold,
            )
            if mbd_files:
                logger.warning("Using MBD %s template fallback: %s", kind, mbd_files[0])
                return mbd_files[0]
        return None

    def _find_template_kind(self, kind: str) -> Optional[str]:
        template = self._user_default_template(kind) or self._scan_template(kind)
        if not template:
            logger.error("No %s template found", kind.capitalize())
        return template

    def find_template(self) -> Optional[str]:
        """Find Part template automatically"""
        if self.template_path:
            return self.template_path
        self.template_path = self._find_template_kind("part")
        return self.template_path

    def find_assembly_template(self) -> Optional[str]:
        """Find Assembly template automatically"""
        if self.assembly_template_path:
            return self.assembly_template_path
        self.assembly_template_path = self._find_template_kind("assembly")
        return self.assembly_template_path

    def find_drawing_template(self) -> Optional[str]:
        """Find Drawing template automatically."""
        if self.drawing_template_path:
            return self.drawing_template_path
        self.drawing_template_path = self._find_template_kind("drawing")
        return self.drawing_template_path

    def create_new_assembly(self):
        """Create a brand new assembly document"""
        template = self.find_assembly_template()
        if not template:
            raise Exception("No Assembly template found")

        logger.info("Creating new assembly document")
        doc = self.app.NewDocument(template, 0, 0, 0)

        if not doc:
            raise Exception("Failed to create assembly document")

        logger.info("✓ New assembly document created")
        return doc

    def open_document(self, file_path: str, doc_type: int):
        """Silently open a document from disk. doc_type: 1=part, 2=assembly, 3=drawing.
        Note: OpenDoc6 activates the opened document."""
        errs = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warns = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        doc = self.app.OpenDoc6(file_path, doc_type, 1, "", errs, warns)  # option 1 = silent
        if not doc:
            raise Exception(
                f"Failed to open document: {file_path} "
                f"(errors={errs.value}, warnings={warns.value})"
            )
        return doc

    def activate_document(self, title: str):
        """Activate an already-open document by title. Tries the title as given,
        then with document extensions appended (titles may include extensions
        depending on Windows Explorer settings)."""
        err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        candidates = [title]
        if "." not in title:
            candidates += [f"{title}.SLDPRT", f"{title}.SLDASM", f"{title}.SLDDRW"]
        for name in candidates:
            doc = self.app.ActivateDoc3(name, False, 2, err)  # 2 = rebuild on activation
            if doc:
                return doc
        raise Exception(f"Could not activate document: {title}")
    
    def create_new_part(self):
        """Create a brand new part document"""
        template = self.find_template()
        if not template:
            raise Exception("No Part template found")
        
        logger.info("Creating new part document")
        doc = self.app.NewDocument(template, 0, 0, 0)
        
        if not doc:
            raise Exception("Failed to create part document")
        
        logger.info("✓ New part document created")
        return doc
    
    def get_active_doc(self):
        """Get the currently active document"""
        return self.app.ActiveDoc if self.app else None

    def is_alive(self) -> bool:
        """Check if the COM connection to SolidWorks is still alive."""
        if not self.app:
            return False
        try:
            _ = self.app.RevisionNumber
            return True
        except Exception:
            return False

    def reconnect(self) -> bool:
        """Drop the current COM handle and establish a fresh connection."""
        logger.warning("Attempting to reconnect to SolidWorks...")
        self.app = None
        self.template_path = None
        self.assembly_template_path = None
        self.drawing_template_path = None
        # Brief pause to let SolidWorks stabilize if it just restarted
        time.sleep(2)
        return self.connect()

    def close_all_docs(self):
        """Close all open documents without saving.

        Bounded loop: QuitDoc can decline to close a document (e.g. a modal
        dialog is up), in which case ActiveDoc keeps returning the same doc —
        an unbounded `while True` here would spin forever.
        """
        if not self.is_alive():
            return
        closed = 0
        last_title = None
        for _ in range(50):
            doc = self.app.ActiveDoc
            if not doc:
                break
            title = doc.GetTitle
            if title == last_title:
                logger.warning(
                    f"QuitDoc did not close '{title}'; aborting close_all_docs"
                )
                break
            self.app.QuitDoc(title)
            last_title = title
            closed += 1
        else:
            logger.warning("close_all_docs hit the 50-iteration bound")
        if closed:
            logger.info(f"Closed {closed} document(s)")

    def get_sw_version(self) -> str:
        """Return the SolidWorks revision string, or 'unknown'."""
        if not self.is_alive():
            return "unknown"
        try:
            return str(self.app.RevisionNumber)
        except Exception:
            return "unknown"

    def ensure_connection(self):
        """Ensure we have a live connection, reconnecting if needed."""
        if self.is_alive():
            return
        logger.warning("SolidWorks COM connection is dead, reconnecting...")
        if not self.reconnect():
            raise Exception(
                "Failed to connect to SolidWorks. "
                "Ensure SolidWorks is running and responsive."
            )

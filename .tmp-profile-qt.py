import json,time
from pathlib import Path
from PySide6.QtWidgets import QApplication
class TimedApplication(QApplication):
    def __init__(self):
        super().__init__([])
        self.slow=[]
    def notify(self, receiver, event):
        active=getattr(self,"execution_profile_active",False)
        started=time.perf_counter()
        kind=event.type().name
        name=type(receiver).__name__
        try:
            return super().notify(receiver,event)
        finally:
            elapsed=time.perf_counter()-started
            if active and elapsed>.015:
                parents=[]
                target=receiver.views()[0] if name=='GraphicsScene' and receiver.views() else receiver
                while target is not None:
                    parents.append(type(target).__name__+':'+target.objectName())
                    target=target.parent()
                self.slow.append({'object':name,'parents':parents,'event':kind,'seconds':elapsed})
app=TimedApplication()
import pytest
code=pytest.main(['tests/test_source_review_execution_startup.py','-q','-p','no:cacheprovider','--basetemp=.tmp-startup-qt-active'])
rows=sorted(app.slow,key=lambda x:x['seconds'],reverse=True)
Path('docs/audits/2026-10-06-source-review/startup-qt-events.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
print(json.dumps(rows[:25],indent=2))
raise SystemExit(code)

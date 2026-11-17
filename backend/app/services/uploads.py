from __future__ import annotations

import time
import uuid
from pathlib import Path

from app.schemas.llm import ImageInput
from app.schemas.security import UserContext


class LocalUploadStore:
    def __init__(self,root:str,ttl_seconds:int=86400):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True);self.ttl=ttl_seconds
        self._index:dict[str,tuple[str,str,str,float]]={}
    async def save(self,user:UserContext,data:bytes,mime_type:str)->str:
        if mime_type not in {"image/png","image/jpeg","image/webp"}:raise ValueError("Unsupported upload image type")
        upload_id=uuid.uuid4().hex;relative=Path(user.tenant_id)/user.employee_id/f"{upload_id}.img";path=self.root/relative
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        self._index[upload_id]=(user.tenant_id,user.employee_id,str(relative),time.time()+self.ttl)
        return upload_id
    async def load(self,user:UserContext,upload_id:str)->ImageInput|None:
        record=self._index.get(upload_id)
        if not record or record[0]!=user.tenant_id or record[1]!=user.employee_id or record[3]<time.time():return None
        path=(self.root/record[2]).resolve()
        if self.root not in path.parents or not path.is_file():return None
        mime="image/png" if path.suffix.lower()==".png" else "image/jpeg"
        return ImageInput(data=path.read_bytes(),mime_type=mime)

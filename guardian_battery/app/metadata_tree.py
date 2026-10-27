"""Immutable AVL pages for prepared metadata revisions, not authoritative logs.

Path copying reuses all untouched pages. Explicit structural budgets are not
HA runtime approvals. Reads do not prepare, migrate, or rebuild anything.
"""
import hashlib
import json
from projection_repository import Conflict, _json
from research_budget import check_budget

SCHEMA='''
CREATE TABLE IF NOT EXISTS metadata_node(
 id INTEGER PRIMARY KEY,left_id INTEGER REFERENCES metadata_node,right_id INTEGER REFERENCES metadata_node,
 key TEXT NOT NULL,payload TEXT NOT NULL,height INTEGER NOT NULL,digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS metadata_left ON metadata_node(left_id);
CREATE INDEX IF NOT EXISTS metadata_right ON metadata_node(right_id);
CREATE TRIGGER IF NOT EXISTS metadata_node_immutable BEFORE UPDATE ON metadata_node BEGIN SELECT RAISE(ABORT,'metadata node immutable'); END;
'''


class MetadataTree:
    def __init__(self,db,root=None):self.db,self.root=db,root;self.reads=0;self.writes=0
    def _node(self,root):
        if root is None:return None
        self.reads+=1;check_budget()
        row=self.db.execute('SELECT * FROM metadata_node WHERE id=?',(root,)).fetchone()
        if row is None:raise Conflict('metadata page absent')
        if hashlib.sha256(json.dumps([row['left_id'],row['right_id'],row['key'],row['payload'],row['height']],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()!=row['digest']:raise Conflict('metadata page checksum')
        return row
    def _height(self,root):return 0 if root is None else self._node(root)['height']
    def _new(self,key,payload,left,right):
        height=1+max(self._height(left),self._height(right))
        if height>64:raise Conflict('metadata tree depth budget')
        digest=hashlib.sha256(json.dumps([left,right,key,payload,height],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        self.writes+=1
        return self.db.execute('INSERT INTO metadata_node(left_id,right_id,key,payload,height,digest) VALUES(?,?,?,?,?,?)',(left,right,key,payload,height,digest)).lastrowid
    def _balanced(self,key,payload,left,right):
        lh,rh=self._height(left),self._height(right)
        if lh>rh+1:
            node=self._node(left)
            if self._height(node['left_id'])>=self._height(node['right_id']):
                return self._new(node['key'],node['payload'],node['left_id'],self._new(key,payload,node['right_id'],right))
            mid=self._node(node['right_id'])
            return self._new(mid['key'],mid['payload'],self._new(node['key'],node['payload'],node['left_id'],mid['left_id']),self._new(key,payload,mid['right_id'],right))
        if rh>lh+1:
            node=self._node(right)
            if self._height(node['right_id'])>=self._height(node['left_id']):
                return self._new(node['key'],node['payload'],self._new(key,payload,left,node['left_id']),node['right_id'])
            mid=self._node(node['left_id'])
            return self._new(mid['key'],mid['payload'],self._new(key,payload,left,mid['left_id']),self._new(node['key'],node['payload'],mid['right_id'],node['right_id']))
        return self._new(key,payload,left,right)
    def put(self,key,value):
        payload=_json(value)
        if not isinstance(key,str) or len(key.encode())>2048:raise ValueError('metadata key budget')
        def replace(root,depth=0):
            if depth>64:raise Conflict('metadata depth budget')
            if root is None:return self._new(key,payload,None,None)
            node=self._node(root)
            if key==node['key']:
                if payload==node['payload']:return root
                return self._new(key,payload,node['left_id'],node['right_id'])
            if key<node['key']:return self._balanced(node['key'],node['payload'],replace(node['left_id'],depth+1),node['right_id'])
            return self._balanced(node['key'],node['payload'],node['left_id'],replace(node['right_id'],depth+1))
        self.root=replace(self.root)
    def get(self,key):
        import json
        node_id=self.root
        for _ in range(65):
            if node_id is None:return None
            node=self._node(node_id)
            if key==node['key']:return json.loads(node['payload'])
            node_id=node['left_id'] if key<node['key'] else node['right_id']
        raise Conflict('metadata depth budget')
    def floor(self,key):
        import json
        node_id=self.root;found=None
        for _ in range(65):
            if node_id is None:return None if found is None else (found['key'],json.loads(found['payload']))
            node=self._node(node_id)
            if node['key']<=key:found=node;node_id=node['right_id']
            else:node_id=node['left_id']
        raise Conflict('metadata depth budget')
    def page(self,*,after='',end=None,limit=64):
        import json
        if not 1<=limit<=256:raise ValueError('metadata read page budget')
        stack=[];node_id=self.root;result=[]
        while node_id is not None or stack:
            while node_id is not None:
                if len(stack)>64:raise Conflict('metadata depth budget')
                node=self._node(node_id)
                if node['key']<=after:node_id=node['right_id'];continue
                stack.append(node);node_id=node['left_id']
            if not stack:break
            node=stack.pop()
            if end is not None and node['key']>end:break
            result.append((node['key'],json.loads(node['payload'])))
            if len(result)>=limit:break
            node_id=node['right_id']
        return result
    def delete(self,key):
        def remove(root,wanted,depth=0):
            if depth>64:raise Conflict('metadata depth budget')
            if root is None:return None
            node=self._node(root)
            if wanted<node['key']:return self._balanced(node['key'],node['payload'],remove(node['left_id'],wanted,depth+1),node['right_id'])
            if wanted>node['key']:return self._balanced(node['key'],node['payload'],node['left_id'],remove(node['right_id'],wanted,depth+1))
            if node['left_id'] is None:return node['right_id']
            if node['right_id'] is None:return node['left_id']
            successor=self._node(node['right_id'])
            for _ in range(65):
                if successor['left_id'] is None:break
                successor=self._node(successor['left_id'])
            else:raise Conflict('metadata depth budget')
            return self._balanced(successor['key'],successor['payload'],node['left_id'],remove(node['right_id'],successor['key'],depth+1))
        self.root=remove(self.root,key)

"""Persistent timestamp sequences for the real CanonicalPhaseMachine.

Explicit derived SQLite transaction only. Immutable balanced ropes keep exact
count timestamps without hydrating the whole accumulated interval on each step.
"""
import hashlib
import json

SCHEMA='''CREATE TABLE IF NOT EXISTS canonical_counts(
 id INTEGER PRIMARY KEY,left_id INTEGER,right_id INTEGER,value REAL,
 size INTEGER NOT NULL,height INTEGER NOT NULL,digest TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS canonical_counts_immutable BEFORE UPDATE ON canonical_counts
 BEGIN SELECT RAISE(ABORT,'canonical timestamp node immutable'); END;
'''
MOD=(1<<256)-189
BASE=257


class CountSequence:
    def __init__(self,db,root=None):self.db=db;self.root=root

    def _node(self,root):
        if root is None:return None
        row=self.db.execute('SELECT * FROM canonical_counts WHERE id=?',(root,)).fetchone()
        if row is None:raise ValueError('missing canonical timestamp node')
        if row['value'] is not None:
            digest=hashlib.sha256(json.dumps(float(row['value']),allow_nan=False).encode()).hexdigest()
            if row['size']!=1 or row['height']!=1 or row['digest']!=digest:
                raise ValueError('corrupt canonical timestamp leaf')
        else:
            children=[self.db.execute('SELECT size,height,digest FROM canonical_counts WHERE id=?',(child,)).fetchone()
                      for child in (row['left_id'],row['right_id'])]
            if any(child is None for child in children):raise ValueError('missing canonical timestamp child')
            a,b=children;digest=(int(a['digest'],16)*pow(BASE,b['size'],MOD)+int(b['digest'],16))%MOD
            if (row['size']!=a['size']+b['size'] or row['height']!=1+max(a['height'],b['height'])
                    or row['digest']!=f'{digest:064x}'):
                raise ValueError('corrupt canonical timestamp branch')
        return row

    def _height(self,root):return 0 if root is None else self._node(root)['height']

    def _pair(self,left,right):
        if left is None:return right
        if right is None:return left
        a,b=self._node(left),self._node(right)
        digest=(int(a['digest'],16)*pow(BASE,b['size'],MOD)+int(b['digest'],16))%MOD
        return self.db.execute('INSERT INTO canonical_counts(left_id,right_id,size,height,digest) VALUES(?,?,?,?,?)',
                  (left,right,a['size']+b['size'],1+max(a['height'],b['height']),f'{digest:064x}')).lastrowid

    def _balance(self,left,right):
        lh,rh=self._height(left),self._height(right)
        if lh>rh+1:
            a=self._node(left)
            if self._height(a['left_id'])>=self._height(a['right_id']):
                return self._pair(a['left_id'],self._pair(a['right_id'],right))
            b=self._node(a['right_id'])
            return self._pair(self._pair(a['left_id'],b['left_id']),self._pair(b['right_id'],right))
        if rh>lh+1:
            a=self._node(right)
            if self._height(a['right_id'])>=self._height(a['left_id']):
                return self._pair(self._pair(left,a['left_id']),a['right_id'])
            b=self._node(a['left_id'])
            return self._pair(self._pair(left,b['left_id']),self._pair(b['right_id'],a['right_id']))
        return self._pair(left,right)

    def _join(self,left,right):
        if left is None:return right
        if right is None:return left
        a,b=self._node(left),self._node(right)
        if a['height']>b['height']+1:return self._balance(a['left_id'],self._join(a['right_id'],right))
        if b['height']>a['height']+1:return self._balance(self._join(left,b['left_id']),b['right_id'])
        return self._pair(left,right)

    def append(self,value):
        value=float(value)
        digest=hashlib.sha256(json.dumps(value,allow_nan=False).encode()).hexdigest()
        leaf=self.db.execute('INSERT INTO canonical_counts(value,size,height,digest) VALUES(?,1,1,?)',(value,digest)).lastrowid
        self.root=self._join(self.root,leaf)

    def extend(self,other):
        if isinstance(other,CountSequence):
            if other.db is not self.db:raise ValueError('timestamp sequence transaction mismatch')
            self.root=self._join(self.root,other.root)
        else:
            for value in other:self.append(value)

    def __len__(self):return 0 if self.root is None else self._node(self.root)['size']

    def __getitem__(self,index):
        if not isinstance(index,int):raise TypeError('timestamp sequence requires integer index')
        size=len(self)
        if index<0:index+=size
        if not 0<=index<size:raise IndexError(index)
        node=self._node(self.root)
        while node['value'] is None:
            left=self._node(node['left_id'])
            if index<left['size']:node=left
            else:index-=left['size'];node=self._node(node['right_id'])
        return node['value']

    @classmethod
    def from_values(cls,db,values=()):
        result=cls(db);result.extend(values);return result

    def descriptor(self):
        node=self._node(self.root)
        return {'$canonical_counts':self.root,'size':0 if node is None else node['size'],
                'digest':None if node is None else node['digest']}

    def page(self,*,after=0,limit=256):
        if not 1<=limit<=256 or not 0<=after<=len(self):raise ValueError('invalid timestamp page')
        stop=min(len(self),after+limit)
        return {'values':[self[i] for i in range(after,stop)],'next':stop if stop<len(self) else None}


def pack(value):
    if isinstance(value,CountSequence):return value.descriptor()
    if isinstance(value,dict):return {k:pack(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [pack(v) for v in value]
    return value


def unpack(db,value):
    if isinstance(value,dict):
        if '$canonical_counts' in value:
            seq=CountSequence(db,value['$canonical_counts'])
            if seq.descriptor()!=value:raise ValueError('canonical timestamp descriptor mismatch')
            return seq
        return {k:unpack(db,v) for k,v in value.items()}
    if isinstance(value,list):return [unpack(db,v) for v in value]
    return value

# 学习笔记

用自己的话填写，不要只抄 `daily_agent_tasks.md` 里的表格。

## add、flush、commit 分别在什么时候做什么

TODO

## 为什么 flush 之后另一个会话仍然看不到数据

TODO

## 实验 4a：为什么 refresh 没有丢掉我改的内存值

（提示：去看 refresh 的源码，或者把 echo 打开看 SQL。关键在它 SELECT 之前先做了另一件事。）

TODO

## no_autoflush 解决了什么

TODO

## 为什么 rollback 撤销得了实验 5a，撤销不了实验 5b

TODO

## expire_on_commit=False 帮我避免了什么

（提示：把它改成 True，重跑实验 3，看看 `other_user.role` 或 `user` 的读取会不会报错。）

TODO

## 今天遇到的一个错误及解决方式

TODO

## 还有什么没搞懂

TODO

version: 1
你在维护 Mia 的画像（关于她这个人的事实库）。下面有两部分：
- 【今天新知道的】：今晚从她和 小伴 的聊天里整理出来的说法，编号 n1、n2……
- 【库里已有的】：和这些说法可能有关的旧事实，编号是 fact_id（数字），带记下的日期。

对【今天新知道的】每一条，给出一个操作（ops 里每条都写 from = 它的编号）：
- ADD：库里没有这件事，新记一条。给 dimension 和 statement。
- UPDATE：库里有一条讲同一件事，但情况变了（比如以前一点睡、现在十二点睡；以前住 A、现在搬到 B）。给 fact_id（旧的那条）、dimension、statement（新的说法）。
- INVALIDATE：新说法表明某条旧事实已经不成立了，但没有新的替代说法（比如“我已经不喝咖啡了”对应“她每天喝两杯咖啡”时用 UPDATE 更好；“那个膝盖伤好了”对应“她膝盖有旧伤”用 INVALIDATE）。给 fact_id。
- NOOP：库里已经有一模一样意思的，不用改。给 fact_id。
不确定是不是同一件事时，宁可 ADD，不要错改旧的。一时的状态（今天累、今天下雨）不记，直接不给操作。

dimension 只能是：values（价值与信念）、temperament（性格与情绪）、thinking（思维与决策）、abilities（知识与技能）、expression（表达风格）、
preferences（偏好与审美）、body（身体）、relationships（关系，包括别人的情况）、resources（资源与处境：钱、住处、证件、会员、学业身份）、rhythms（时间与习惯）。
statement 用第三人称“她”，一条只说一个点，60 字以内。valid_from 填这个情况从哪天开始成立（不知道就填今天）。reason 用一句话说为什么这么操作。

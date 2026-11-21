# 模块说明：资料字段契约：根据学生 JSON 和教师模板定义控件、身份范围与公开规则。
"""Shared field contract based on candidate.json and teacher import workbook."""
FIELDS = [
    ("name", "姓名", "text", "both", "public"),
    ("avatar_name", "分身名称", "text", "both", "public"),
    ("preferred_name", "常用称呼", "text", "both", "public"),
    ("gender", "性别", "select", "both", "private"),
    ("student_id", "学号（选填）", "text", "student", "private"),
    ("employee_id", "教师工号（选填）", "text", "teacher", "private"),
    ("location", "所在地", "text", "both", "private"),
    ("workstations", "工位（选填，校区、楼宇、房间等）", "flextext", "both", "private"),
    ("bio", "个人简介", "textarea", "both", "public"),
    ("organization", "所属学校 / 组织", "text", "both", "public"),
    ("specialty", "专业 / 研究方向", "textarea", "both", "public"),
    ("email", "邮箱", "text", "both", "private"),
    ("phone", "手机号 / 电话（选填）", "text", "both", "private"),
    ("contact", "其他联系方式（选填）", "text", "both", "private"),
    ("homepage", "个人主页", "text", "both", "public"),
    ("current_advisor", "当前导师", "text", "student", "private"),
    ("experience", "个人经历", "textarea", "both", "public"),
    ("education", "教育经历", "flextext", "both", "public"),
    ("skills", "技能与证据", "flextext", "both", "public"),
    ("current_projects", "当前项目", "flextext", "both", "public"),
    ("projects", "项目经历", "flextext", "both", "public"),
    ("certifications", "证书 / 资格", "strings", "both", "public"),
    ("academic_highlights", "学术亮点 / 成绩与荣誉", "strings", "both", "private"),
    ("interests", "兴趣爱好", "strings", "both", "public"),
    ("highest_degree", "最高学历", "text", "teacher", "public"),
    ("title", "职称", "text", "teacher", "public"),
    ("supervisor_qualification", "导师资格", "text", "teacher", "public"),
    ("appointments", "任职（机构、角色、起止日期）", "flextext", "teacher", "public"),
    ("research_outputs", "科研成果", "strings", "teacher", "public"),
    ("supervised_students", "指导学生", "flextext", "teacher", "private"),
    ("other", "其他资料", "textarea", "both", "private"),
    ("supplementary_text", "补充说明（选填）", "textarea", "both", "private"),
]
FIELD_NAMES = {f[0] for f in FIELDS}
FIXED_PUBLIC_FIELDS = frozenset({'name', 'avatar_name'})


# 职责：生成前端学生与教师资料字段定义。
# 输入：读取 FIELDS 中键、标签、控件类型、适用身份及默认可见性。
# 结果与约束：返回目录及固定公开标记；姓名和分身名称不允许选私密，文字内容仍按普通审核流程修改。
def catalog():
    return [{"key": k, "label": label, "type": kind, "audience": audience, "visibility": visibility,
             "fixed_public": k in FIXED_PUBLIC_FIELDS,
             **({"options": ["男", "女", "其他", "不愿透露"]} if k == "gender" else {})}
            for k, label, kind, audience, visibility in FIELDS]


# 职责：筛选当前身份允许公开的个人资料字段。
# 输入：profile 为已审核资料，visibility 指定每项可见性，未指定时使用目录默认值。
# 结果与约束：姓名及分身名称始终公开，兼容旧私密标记；其余私密或不适用字段剔除，未知扩展字段不自动公开。
def public_profile(profile):
    visibility = profile.get("visibility", {})
    person_type = profile.get("person_type", "student")
    return {key: profile[key] for key, _, _, audience, default in FIELDS
            if key in profile and audience in ("both", person_type)
            and (key in FIXED_PUBLIC_FIELDS or visibility.get(key, default) == "public")}


# 职责：将参考学生 JSON 映射为本系统资料结构。
# 输入：candidate 为参考对象，student_id 为明确提供的学生学号。
# 结果与约束：返回映射结果而不写库或公开；示例信息不能自动灌入所有用户账户。
def from_candidate(candidate, student_id):
    """Reference mapping only; never automatically publish someone's sample data."""
    identity = candidate.get("identity", {})
    return {"person_type": "student", "student_id": student_id, "name": identity.get("name", ""),
            "preferred_name": identity.get("preferred_name", ""), "location": identity.get("location", ""),
            "bio": candidate.get("introduction", ""),
            **{k: candidate[k] for k in ("current_advisor", "current_projects", "skills", "projects", "education", "certifications", "academic_highlights", "interests") if k in candidate}}
